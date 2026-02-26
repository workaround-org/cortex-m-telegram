# cortex-m-telegram-connector

A lightweight Telegram bot that bridges [Telegram](https://telegram.org/) to [Cortex-M](https://github.com/workaround-org/cortex-m) using the [Connector Protocol](https://github.com/workaround-org/cortex-m/wiki/Connector-Protocol).

Users send messages to the Telegram bot; the connector forwards them to Cortex-M over a persistent WebSocket and replies with the assistant's response.

---

## How it works

```
Telegram user
     │  (message)
     ▼
python-telegram-bot (polling)
     │  assistant.message.inbound  CloudEvent
     ▼
Cortex-M WebSocket (/api/cortex-m/v1/connector/<sessionId>)
     │  assistant.message.outbound CloudEvent
     ▼
python-telegram-bot
     │  (reply)
     ▼
Telegram user
```

1. On startup the connector calls `GET /connector` to obtain a session UUID.
2. It opens a WebSocket to `/connector/<sessionId>`.
3. Every Telegram text message is wrapped in a `assistant.message.inbound` [CloudEvent 1.0](https://cloudevents.io/) and sent over the WebSocket.
4. Cortex-M replies with an `assistant.message.outbound` CloudEvent; the connector forwards the text back to the Telegram chat.
5. The WebSocket is kept alive and reconnected automatically on failure (exponential backoff, 2 s → 60 s).

---

## Requirements

- Python 3.12+
- A [Telegram bot token](https://core.telegram.org/bots/tutorial) from [@BotFather](https://t.me/BotFather)
- A running Cortex-M instance

---

## Configuration

All configuration is via environment variables.

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_TOKEN` | ✅ | — | Telegram bot token from BotFather |
| `TELEGRAM_ALLOWLIST` | ❌ | — | Comma-separated list of allowed user IDs or usernames (empty = allow all) |
| `CORTEX_M_URL` | ✅ | — | Base URL of the Cortex-M API, e.g. `http://cortex-m:8080/api/cortex-m/v1` |
| `CONNECTOR_ID` | ❌ | `telegram-1` | Stable identifier for this connector instance (appears in CloudEvents `source`) |

---

## Running with Docker Compose

Create a `.env` file:

```env
TELEGRAM_TOKEN=123456:ABC-DEF...
CORTEX_M_URL=http://cortex-m:8080/api/cortex-m/v1
CONNECTOR_ID=telegram-1
```

Then start the connector:

```bash
docker compose up -d
```

The `docker-compose.yml` references the pre-built image from GHCR. To build locally instead, replace `image:` with `build: .` or run:

```bash
docker compose up -d --build
```

---

## Running locally (without Docker)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export TELEGRAM_TOKEN=123456:ABC-DEF...
export CORTEX_M_URL=http://localhost:8080/api/cortex-m/v1
export CONNECTOR_ID=telegram-1

python connector.py
```

---

## Docker image

Pre-built images are published to GitHub Container Registry on every push to `main` and on version tags.

```bash
# Latest main branch build
docker pull ghcr.io/workaround-org/cortex-m-telegram:main

# Specific version
docker pull ghcr.io/workaround-org/cortex-m-telegram:v1.2.3
```

---

## Dependencies

| Package | Purpose |
|---|---|
| [`python-telegram-bot`](https://python-telegram-bot.org/) | Async Telegram Bot API client |
| [`websockets`](https://websockets.readthedocs.io/) | WebSocket client for Cortex-M |
| [`httpx`](https://www.python-httpx.org/) | HTTP client for session token request |

---

## See also

- [Connector Protocol](https://github.com/workaround-org/cortex-m/wiki/Connector-Protocol) — WebSocket message format and lifecycle
