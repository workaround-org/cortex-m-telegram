# Copilot Instructions

## Project Overview

Single-file Python service (`connector.py`) that bridges Telegram to [Cortex-M](https://github.com/workaround-org/cortex-m) using its Connector Protocol over WebSocket + CloudEvents 1.0.

## Architecture

The connector runs two concurrent async tasks inside one process:

- **`ws_loop()`** — maintains the WebSocket connection to Cortex-M. On startup it calls `GET /connector` for a session UUID, then opens `WS /connector/<sessionId>`. Reconnects automatically with exponential backoff (2 s → 60 s). On reconnect it re-sends any messages that were in-flight.
- **`handle_message()`** — Telegram update handler. Each incoming message creates an `asyncio.Future`, stores it in `_pending[conversationId]`, and enqueues the CloudEvent JSON onto `_send_queue`.

The two tasks communicate through two shared data structures:
- `_send_queue` (`asyncio.Queue`) — carries raw CloudEvent JSON strings from the Telegram handler to the WebSocket sender.
- `_pending` (`dict[str, tuple[Future, str]]`) — maps `conversationId → (future, payload_json)`. The future is resolved by `_receiver()` when an `assistant.message.outbound` event arrives. The payload is kept so it can be re-queued after a reconnect.

`asyncio.shield(fut)` is used so that a reply timeout does **not** cancel the future — the pending entry is dropped from `_pending` on timeout, but if Cortex-M later replies the WebSocket receiver just logs an "unknown conversationId" warning instead of crashing.

## Connector Protocol (Cortex-M)

Full spec: `wiki/Connector-Protocol.md`

| Step | Action |
|---|---|
| 1 | `GET /connector` → plain-text session UUID |
| 2 | `WS /connector/<sessionId>` → persistent connection |
| 3 | Send `assistant.message.inbound` CloudEvent (see `_build_inbound_event`) |
| 4 | Receive `assistant.message.outbound` CloudEvent; match on `conversationId` |

`conversationId` == `roomId` == Telegram `chat_id` (string). Unknown event types are silently ignored.

## Configuration (env vars)

| Variable | Default | Notes |
|---|---|---|
| `CORTEX_M_URL` | *(required)* | e.g. `http://cortex-m:8080/api/cortex-m/v1` |
| `TELEGRAM_TOKEN` | *(required)* | From BotFather |
| `CONNECTOR_ID` | `telegram-1` | Used in CloudEvent `source` as `urn:connector:<id>` |
| `TELEGRAM_ALLOWLIST` | *(empty = open)* | Comma-separated user IDs or usernames |
| `CORTEX_M_TIMEOUT` | `180` | Seconds to wait for a reply before sending the timeout message |

## Running Locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export TELEGRAM_TOKEN=... CORTEX_M_URL=...
python connector.py
```

## Docker

```bash
# Build and run
docker compose up -d --build

# Rebuild after changes to connector.py
docker compose up -d --build telegram-connector
```

The image is multi-arch (amd64 + arm64). CI builds and pushes to GHCR on every push to `main` and on `v*.*.*` tags.

## Commit Messages

Use [Gitmoji](https://gitmoji.dev/) at the start of every commit message. Examples:

- `✨ Add CORTEX_M_TIMEOUT env var`
- `🐛 Fix future not resolved on reconnect`
- `📝 Update README with new config options`
- `🔧 Bump websockets to 14.*`

## Key Conventions

- **All configuration is via environment variables.** No config files.
- **`connector.py` is intentionally a single file.** Keep it that way unless there is a strong reason to split.
- **Dependencies are pinned to major versions** in `requirements.txt` (e.g. `websockets==13.*`). Follow this pattern when adding new dependencies.
- **Markdown replies use a fallback:** `reply_text(..., parse_mode="Markdown")` is wrapped in a try/except that retries as plain text — preserve this pattern for any new reply paths.
- **No tests exist** in this repository. Validate changes by running the connector against a real or mocked Cortex-M instance.
