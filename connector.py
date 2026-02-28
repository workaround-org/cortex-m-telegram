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
import uuid
from datetime import datetime, timezone

import httpx
import websockets
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

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
            await update.message.reply_text(reply, parse_mode="Markdown")
        except Exception:
            # Fallback to plain text if the response contains malformed Markdown
            await update.message.reply_text(reply)
    except asyncio.TimeoutError as exc:
        _pending.pop(conversation_id, None)
        log.warning("No reply for chat %s: %s", chat_id, exc)
        await update.message.reply_text("⏳ Cortex-M did not respond in time. Please try again.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        log.info("Telegram bot polling started")
        await ws_loop()  # runs forever; reconnects on failure
        await app.updater.stop()
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
