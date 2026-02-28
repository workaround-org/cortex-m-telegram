#!/usr/bin/env python3
"""Telegram → Cortex-M connector.

Implements the Connector Protocol:
  1. GET /connector          → obtain sessionId
  2. WS  /connector/<id>    → open WebSocket
  3. Send assistant.message.inbound  CloudEvent for every Telegram message
  4. Receive assistant.message.outbound and reply to the Telegram chat
"""

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from html import escape
from html.parser import HTMLParser

import httpx
import websockets
from markdown_it import MarkdownIt
from telegram import Bot, Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Markdown → Telegram HTML conversion
# ---------------------------------------------------------------------------
_md = MarkdownIt().enable("table")


class _TelegramHTMLConverter(HTMLParser):
    """Converts standard HTML (from markdown-it-py) to Telegram-compatible HTML."""

    def __init__(self) -> None:
        super().__init__()
        self._out: list[str] = []
        self._stack: list[str] = []
        # table state
        self._in_table = False
        self._table_rows: list[list[str]] = []
        self._current_row: list[str] = []
        self._current_cell: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        attrs_d = dict(attrs)
        self._stack.append(tag)
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._out.append("<b>")
        elif tag in ("strong", "b"):
            self._out.append("<b>")
        elif tag in ("em", "i"):
            self._out.append("<i>")
        elif tag == "u":
            self._out.append("<u>")
        elif tag in ("s", "del", "strike"):
            self._out.append("<s>")
        elif tag == "code" and "pre" not in self._stack[:-1]:
            self._out.append("<code>")
        elif tag == "pre":
            self._out.append("<pre>")
        elif tag == "a" and "href" in attrs_d:
            self._out.append(f'<a href="{escape(attrs_d["href"])}">')
        elif tag == "blockquote":
            self._out.append("<blockquote>")
        elif tag == "br":
            self._out.append("\n")
        elif tag == "table":
            self._in_table = True
            self._table_rows = []

    def handle_endtag(self, tag: str) -> None:
        if self._stack and self._stack[-1] == tag:
            self._stack.pop()
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._out.append("</b>\n\n")
        elif tag in ("strong", "b"):
            self._out.append("</b>")
        elif tag in ("em", "i"):
            self._out.append("</i>")
        elif tag == "u":
            self._out.append("</u>")
        elif tag in ("s", "del", "strike"):
            self._out.append("</s>")
        elif tag == "code" and "pre" not in self._stack:
            self._out.append("</code>")
        elif tag == "pre":
            self._out.append("</pre>\n")
        elif tag == "a":
            self._out.append("</a>")
        elif tag == "blockquote":
            self._out.append("</blockquote>\n")
        elif tag == "p":
            self._out.append("\n\n")
        elif tag in ("th", "td"):
            self._current_row.append("".join(self._current_cell).strip())
            self._current_cell = []
        elif tag == "tr":
            self._table_rows.append(self._current_row)
            self._current_row = []
        elif tag == "table":
            self._in_table = False
            self._out.append(self._render_table())

    def handle_data(self, data: str) -> None:
        if self._in_table and self._stack and self._stack[-1] in ("th", "td"):
            self._current_cell.append(escape(data))
        elif not self._in_table:
            self._out.append(escape(data))

    def _render_table(self) -> str:
        if not self._table_rows:
            return ""
        col_widths: list[int] = []
        for row in self._table_rows:
            for i, cell in enumerate(row):
                w = len(cell)
                if i >= len(col_widths):
                    col_widths.append(w)
                else:
                    col_widths[i] = max(col_widths[i], w)
        lines: list[str] = []
        for ri, row in enumerate(self._table_rows):
            cells = [cell.ljust(col_widths[i]) if i < len(col_widths) else cell for i, cell in enumerate(row)]
            lines.append("| " + " | ".join(cells) + " |")
            if ri == 0:
                sep = "-+-".join("-" * w for w in col_widths)
                lines.append(f"|-{sep}-|")
        return "<pre>" + escape("\n".join(lines)) + "</pre>\n\n"

    def result(self) -> str:
        text = "".join(self._out)
        return re.sub(r"\n{3,}", "\n\n", text).strip()


def _md_to_telegram_html(text: str) -> str:
    """Convert Markdown to Telegram-compatible HTML."""
    html = _md.render(text)
    converter = _TelegramHTMLConverter()
    converter.feed(html)
    return converter.result()

# ---------------------------------------------------------------------------
# Configuration (from environment)
# ---------------------------------------------------------------------------
CORTEX_M_URL: str = os.environ["CORTEX_M_URL"].rstrip("/")  # e.g. http://cortex-m:8080/api/cortex-m/v1
TELEGRAM_TOKEN: str = os.environ["TELEGRAM_TOKEN"]
CONNECTOR_ID: str = os.environ.get("CONNECTOR_ID", "telegram-1")
TELEGRAM_ALLOWLIST: str = os.environ.get("TELEGRAM_ALLOWLIST", "")
ALLOWED_USERS: set[str] = {x.strip() for x in TELEGRAM_ALLOWLIST.split(",") if x.strip()}
CORTEX_M_TIMEOUT: int = int(os.environ.get("CORTEX_M_TIMEOUT", "180"))  # seconds; default 3 min
SOURCE: str = f"urn:connector:{CONNECTOR_ID}"

if not ALLOWED_USERS:
    log.warning("⚠️  WARNING: TELEGRAM_ALLOWLIST is not set — the bot is open to EVERYONE!")
    log.warning("⚠️  Set TELEGRAM_ALLOWLIST to a comma-separated list of allowed user IDs or usernames.")

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
# conversationId → (Future[reply_text], payload_json)
_pending: dict[str, tuple[asyncio.Future, str]] = {}

# Queue of raw JSON strings to send over the WebSocket
_send_queue: asyncio.Queue[str] = asyncio.Queue()

# Telegram bot instance (set in main) used for broadcast messages
_bot: Bot | None = None

# Set of chat IDs that have interacted with the bot (used for broadcast)
_known_chats: set[int] = set()


# ---------------------------------------------------------------------------
# Cortex-M WebSocket helpers
# ---------------------------------------------------------------------------

async def _get_session_id() -> str:
    """Call GET /connector and return the plain-text session UUID."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{CORTEX_M_URL}/connector", timeout=10)
        resp.raise_for_status()
        return resp.text.strip()


def _ws_url(session_id: str) -> str:
    base = CORTEX_M_URL.replace("https://", "wss://").replace("http://", "ws://")
    return f"{base}/connector/{session_id}"


def _build_inbound_event(conversation_id: str, room_id: str, text: str) -> str:
    return json.dumps({
        "specversion": "1.0",
        "type": "assistant.message.inbound",
        "source": SOURCE,
        "id": str(uuid.uuid4()),
        "time": datetime.now(timezone.utc).isoformat(),
        "datacontenttype": "application/json",
        "data": {
            "connectorId": CONNECTOR_ID,
            "conversationId": conversation_id,
            "roomId": room_id,
            "text": text,
        },
    })


async def _sender(ws) -> None:
    """Forward messages from the send queue to the WebSocket."""
    while True:
        payload = await _send_queue.get()
        await ws.send(payload)
        log.debug("→ Cortex-M: %s", payload[:120])


async def _receiver(ws) -> None:
    """Receive outbound events from Cortex-M and resolve pending futures."""
    async for raw in ws:
        log.debug("← Cortex-M: %s", raw[:120])
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("Received non-JSON frame, ignoring.")
            continue

        if event.get("type") != "assistant.message.outbound":
            log.debug("Ignoring event type: %s", event.get("type"))
            continue

        data = event.get("data", {})
        conv_id: str | None = data.get("conversationId")
        reply_text: str = data.get("text", "")

        if conv_id and conv_id in _pending:
            fut, _ = _pending.pop(conv_id)
            if not fut.done():
                fut.set_result(reply_text)
        elif conv_id == "broadcast" and _bot is not None:
            log.info("Broadcasting message to %d known chat(s)", len(_known_chats))
            html = _md_to_telegram_html(reply_text)
            for cid in list(_known_chats):
                try:
                    await _bot.send_message(cid, html, parse_mode="HTML")
                except Exception:
                    try:
                        await _bot.send_message(cid, reply_text)
                    except Exception as exc:
                        log.warning("Failed to broadcast to chat %s: %s", cid, exc)
        else:
            log.warning("Received reply for unknown conversationId: %s", conv_id)


async def ws_loop() -> None:
    """Maintain the WebSocket connection to Cortex-M, reconnecting on failure."""
    backoff = 2
    while True:
        try:
            session_id = await _get_session_id()
            url = _ws_url(session_id)
            log.info("Connecting to Cortex-M at %s", url)
            async with websockets.connect(url) as ws:
                log.info("WebSocket connected (session=%s)", session_id)
                backoff = 2  # reset on successful connect
                # Drain stale queue entries, then re-send any messages that were
                # pending when the previous connection was lost.  Both operations
                # are synchronous (no await between them) so no new items can
                # sneak in between the drain and the re-queue.
                while not _send_queue.empty():
                    _send_queue.get_nowait()
                for conv_id, (fut, payload) in list(_pending.items()):
                    if not fut.done():
                        _send_queue.put_nowait(payload)
                        log.info("Re-queued pending message for conversation %s after reconnect", conv_id)
                await asyncio.gather(_sender(ws), _receiver(ws))
        except Exception as exc:
            log.error("WebSocket error (%s) — retrying in %ds", exc, backoff)
            # Leave pending futures alive; their messages will be re-sent once
            # the connection is restored (see re-queue logic above).
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


# ---------------------------------------------------------------------------
# Telegram handler
# ---------------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    _known_chats.add(chat_id)
    text = (update.message.text or "").strip()
    if not text:
        return

    user = update.effective_user
    if ALLOWED_USERS and (
        str(user.id) not in ALLOWED_USERS and
        (not user.username or user.username not in ALLOWED_USERS)
    ):
        log.warning("Unauthorized access attempt from user %s (%s)", user.id, user.username)
        return

    conversation_id = str(chat_id)
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[str] = loop.create_future()

    payload = _build_inbound_event(
        conversation_id=conversation_id,
        room_id=str(chat_id),
        text=text,
    )
    _pending[conversation_id] = (fut, payload)
    await _send_queue.put(payload)
    log.info("Queued inbound event for chat %s", chat_id)

    try:
        reply = await asyncio.wait_for(asyncio.shield(fut), timeout=CORTEX_M_TIMEOUT)
        try:
            html = _md_to_telegram_html(reply)
            await update.message.reply_text(html, parse_mode="HTML")
        except Exception as exc:
            log.warning("Failed to send HTML reply, falling back to plain text: %s", exc)
            await update.message.reply_text(reply)
    except asyncio.TimeoutError as exc:
        _pending.pop(conversation_id, None)
        log.warning("No reply for chat %s: %s", chat_id, exc)
        await update.message.reply_text("⏳ Cortex-M did not respond in time. Please try again.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    global _bot
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async with app:
        _bot = app.bot
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        log.info("Telegram bot polling started")
        await ws_loop()  # runs forever; reconnects on failure
        await app.updater.stop()
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
