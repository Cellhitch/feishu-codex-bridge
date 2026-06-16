# Feishu Codex Bridge

Use Feishu or Lark as a remote control channel for the local Codex app.

The bridge receives Feishu messages, forwards them into Codex, keeps each Feishu chat in a persistent Codex thread, and sends the final Codex answer back to Feishu. It is designed for people who want to operate Codex from mobile or team chat while still using the real local Codex environment on a Mac.

```text
Feishu / Lark chat
        ↓
feishu-codex-bridge
        ↓
local Codex app-server / codex exec
        ↓
Feishu reply + optional files/images
```

## Why This Exists

Codex is powerful when it can see and act in your local development environment. Feishu is convenient when you are away from your desk. This project connects the two:

- Talk to Codex from Feishu.
- Keep the conversation visible in the Codex app.
- Forward local approval prompts back to Feishu.
- Send screenshots, images, and files back through Feishu.
- Preserve a safe local-first architecture without exposing a public Codex API.

## Current Status

This is an MVP, but it is already useful for personal remote control.

| Capability | Status |
|---|---|
| Feishu/Lark websocket messages | Working |
| Persistent Codex app threads | Working |
| Text messages | Working |
| Image and file download from Feishu | Working |
| Image/file upload back to Feishu | Working |
| Feishu approval forwarding | Working |
| Codex thread history loading | Working |
| Live Codex status/tool/reasoning forwarding | Available, opt-in |
| HTTP webhook mode | Available |
| Multi-user production hardening | Not complete |

## Features

- **One Feishu chat, one Codex thread**: each Feishu `chat_id` maps to a persistent Codex thread, unless `BRIDGE_CODEX_FIXED_THREAD_ID` pins all chats to one thread.
- **Codex app-server mode**: uses the local Codex app-server so conversations appear in the Codex UI.
- **Codex app-server event stream**: can optionally replay existing thread history, attach recent Feishu history to the next visible Codex turn, and relay status, plan, tool, and reasoning updates.
- **Safe approval loop**: Codex approval requests can be sent to Feishu; reply `approve` or `deny`.
- **Media support**: downloads Feishu images/files locally and uploads local image/file paths from Codex responses back to Feishu.
- **Fallback delivery**: if normal chat sending fails, the bridge falls back to Feishu message replies.
- **Dry-run mode**: process messages without posting back to Feishu while testing.

## Repository Structure

```text
.
├── pyproject.toml              # package metadata, CLI entry point, dependencies
├── uv.lock                     # locked Python dependency graph
├── .env.example                # safe configuration template
├── src/feishu_codex_bridge/
│   ├── __main__.py             # CLI entrypoint: HTTP server or websocket mode
│   ├── app.py                  # FastAPI webhook, health check, local simulation API
│   ├── approval.py             # Feishu approve/deny flow for Codex local approvals
│   ├── codex_client.py         # codex exec + Codex app-server JSON-RPC clients
│   ├── config.py               # environment-based settings
│   ├── feishu_client.py        # Feishu token, send, reply, upload, download helpers
│   ├── feishu_events.py        # Feishu/Lark event parsing
│   ├── feishu_ws.py            # Feishu long-connection websocket bridge
│   ├── security.py             # Feishu token/signature verification
│   └── thread_store.py         # Feishu chat_id to Codex thread_id mapping
└── tests/                      # unit tests for config, Feishu, Codex, and routing
```

## Requirements

- macOS with the local Codex app installed.
- Python 3.11.
- [`uv`](https://github.com/astral-sh/uv).
- A Feishu or Lark custom app with bot messaging enabled.
- Feishu app credentials in `.env`, shell environment variables, or a local Hermes env file.

## Install

```bash
git clone https://github.com/<owner>/feishu-codex-bridge.git
cd feishu-codex-bridge
UV_CACHE_DIR=.uv-cache uv sync --extra feishu --extra test
cp .env.example .env
```

Edit `.env` and set at least:

```bash
BRIDGE_USE_HERMES_FEISHU_ENV=false
BRIDGE_FEISHU_APP_ID=
BRIDGE_FEISHU_APP_SECRET=
BRIDGE_FEISHU_VERIFICATION_TOKEN=
BRIDGE_FEISHU_ENCRYPT_KEY=
BRIDGE_FEISHU_DOMAIN=feishu
```

If you already keep Feishu credentials in a Hermes env file, leave:

```bash
BRIDGE_USE_HERMES_FEISHU_ENV=true
BRIDGE_HERMES_ENV_PATH=~/.hermes/.env
```

## Smoke Test

Start in safe local HTTP mode. This does not require Feishu to send real messages:

```bash
UV_CACHE_DIR=.uv-cache uv run feishu-codex-bridge
```

Check health:

```bash
curl http://127.0.0.1:8788/health
```

Run a local simulation without Feishu:

```bash
curl -X POST http://127.0.0.1:8788/simulate \
  -H 'content-type: application/json' \
  -d '{"text":"Reply with exactly: Feishu Codex bridge OK"}'
```

## Run Modes

| Mode | Command | Use when |
|---|---|---|
| HTTP server | `uv run feishu-codex-bridge` | Testing locally, webhook deployments, or `/simulate` smoke tests. |
| Feishu websocket | `uv run feishu-codex-bridge feishu-ws -v` | Recommended local remote-control mode. |

## Recommended Run: Feishu Websocket + Codex App Threads

For real remote control, run Feishu long-connection mode with Codex app-server enabled:

```bash
BRIDGE_CODEX_USE_APP_SERVER=true \
BRIDGE_DRY_RUN_REPLIES=false \
UV_CACHE_DIR=.uv-cache uv run feishu-codex-bridge feishu-ws -v
```

This mode:

- Connects directly to Feishu/Lark websocket events.
- Creates or resumes one persistent Codex thread per Feishu chat.
- Shows Feishu conversations in the Codex app.
- Sends Codex final answers back to Feishu.
- Reuses the existing Codex thread without replaying history unless history forwarding is enabled.

Recommended `.env` values for this mode:

```bash
BRIDGE_CODEX_USE_APP_SERVER=true
BRIDGE_DRY_RUN_REPLIES=false
BRIDGE_FEISHU_DELIVERY_MODE=send
BRIDGE_FEISHU_STREAM_UPDATES_ENABLED=false
BRIDGE_FEISHU_SHOW_REASONING=false
BRIDGE_FEISHU_SHOW_HISTORY=false
BRIDGE_FEISHU_PROGRESS_SECONDS=0
```

To force all Feishu chats into one existing Codex thread, set:

```bash
BRIDGE_CODEX_FIXED_THREAD_ID=
```

Leave it blank to use the default one-Feishu-chat-to-one-Codex-thread map.

## Feishu/Lark Setup

1. Create or reuse a Feishu/Lark custom app.
2. Enable bot capability.
3. Enable message receive events.
4. Choose one connection method:
   - **Websocket / long connection**: recommended for local personal use.
   - **HTTP webhook**: useful when exposing the bridge through Cloudflare Tunnel or ngrok.
5. Add credentials to `.env`, or set environment variables:

```bash
BRIDGE_FEISHU_APP_ID=
BRIDGE_FEISHU_APP_SECRET=
BRIDGE_FEISHU_VERIFICATION_TOKEN=
BRIDGE_FEISHU_ENCRYPT_KEY=
BRIDGE_FEISHU_DOMAIN=feishu
```

Use `BRIDGE_FEISHU_DOMAIN=lark` for Lark global apps.

## Configuration

All settings use the `BRIDGE_` environment prefix.

| Setting | Default | Purpose |
|---|---:|---|
| `BRIDGE_DRY_RUN_REPLIES` | `true` | Process locally without sending Feishu replies. |
| `BRIDGE_FEISHU_DELIVERY_MODE` | `send` | `send` posts normal chat messages; `reply` replies under user messages. |
| `BRIDGE_FEISHU_APPROVAL_ENABLED` | `true` | Forward Codex approval prompts to Feishu. |
| `BRIDGE_FEISHU_APPROVAL_TIMEOUT_SECONDS` | `180` | Approval wait timeout. |
| `BRIDGE_FEISHU_PROGRESS_SECONDS` | `0` | Optional repeated plain progress reply interval. Keep `0` when live Codex stream updates are enabled. |
| `BRIDGE_FEISHU_STREAM_UPDATES_ENABLED` | `false` | Forward live Codex status/tool/update events into Feishu. |
| `BRIDGE_FEISHU_SHOW_REASONING` | `false` | Include Codex reasoning summaries in live Feishu updates. |
| `BRIDGE_FEISHU_SHOW_HISTORY` | `false` | Replay loaded Codex thread history back into Feishu after startup. Usually keep this off. |
| `BRIDGE_FEISHU_STREAM_ASSISTANT_DELTAS` | `false` | Forward assistant text deltas before the final answer. |
| `BRIDGE_FEISHU_STREAM_FLUSH_SECONDS` | `2` | Minimum interval for buffered live stream updates. |
| `BRIDGE_FEISHU_SEED_HISTORY_TO_CODEX` | `false` | Fetch recent Feishu chat history and attach it to the next visible Codex turn once per chat. |
| `BRIDGE_FEISHU_HISTORY_MAX_MESSAGES` | `20` | Maximum recent Feishu messages to attach to Codex. |
| `BRIDGE_FEISHU_HISTORY_LOOKBACK_SECONDS` | `86400` | Feishu history lookback window for Codex context. |
| `BRIDGE_FEISHU_HISTORY_MAX_CHARS` | `8000` | Maximum characters copied from Feishu history into Codex. |
| `BRIDGE_CODEX_USE_APP_SERVER` | `false` | Use real Codex app-server thread mode. |
| `BRIDGE_CODEX_MODEL` | `gpt-5.5` | Model passed to Codex. |
| `BRIDGE_CODEX_REASONING_EFFORT` | `medium` | Reasoning effort passed to Codex. |
| `BRIDGE_CODEX_CWD` | `.` | Working directory for Codex tasks. |
| `BRIDGE_CODEX_THREAD_MAP_PATH` | `.codex-thread-map.json` | Persistent Feishu chat → Codex thread map. |
| `BRIDGE_CODEX_FIXED_THREAD_ID` | empty | Optional hard-coded Codex thread ID. When set, all Feishu chats use this thread instead of the map. |
| `BRIDGE_CODEX_LOAD_HISTORY_ON_START` | `true` | Allow Codex thread history replay once per chat after startup. History is read only when `BRIDGE_FEISHU_SHOW_HISTORY=true`. |
| `BRIDGE_MEDIA_DIR` | `feishu-media` | Local folder for downloaded Feishu media. |
| `BRIDGE_MAX_MEDIA_BYTES` | `26214400` | Maximum media download size. |

See `.env.example` for the full list.

## Runtime Files

The bridge creates local runtime state that should stay private and untracked:

| Path | Purpose |
|---|---|
| `.env` | Local credentials and machine-specific settings. |
| `.codex-thread-map.json` | Feishu chat to Codex thread mapping. |
| `feishu-media/` | Downloaded Feishu images/files. |
| `.uv-cache/`, `.venv/`, `.pytest_cache/` | Local Python/test caches. |

These paths are ignored by `.gitignore`.

## Remote Approval Flow

When Codex needs local approval, the bridge sends a Feishu message like:

```text
Codex approval required.

Type: command
Request: open -a "Microsoft Word"

Reply `approve` to allow once, or `deny` to reject.
```

Reply with:

```text
approve
```

or:

```text
deny
```

Approvals are one-time decisions for the current pending request. The bridge does not grant blanket local control.

## Live Codex Updates

By default, Feishu receives the final Codex answer and approval prompts only. To mirror more of the live Codex timeline in Feishu, opt in explicitly:

```bash
BRIDGE_FEISHU_STREAM_UPDATES_ENABLED=true
BRIDGE_FEISHU_SHOW_REASONING=true
BRIDGE_FEISHU_SHOW_HISTORY=false
```

Reasoning and tool-progress messages may include details from local files, commands, or task context. Enable these only in Feishu chats where that extra visibility is acceptable.

## Images and Files

Incoming Feishu images/files are downloaded to `feishu-media/` and passed to Codex as local paths.

Outgoing local paths in Codex responses are detected and uploaded back to Feishu when possible. For example:

```markdown
![screenshot](/absolute/path/to/screenshot.png)
```

The bridge strips the local path from the visible text and sends the image/file through Feishu.

## Safety Model

This bridge is intentionally local-first.

- Secrets are loaded from environment variables or local `.env` files.
- `.env`, logs, media, thread maps, and local caches are ignored by Git.
- `codex exec` uses `--sandbox read-only` by default.
- App-server mode uses Codex approval policies rather than auto-approving local actions.
- Feishu approvals are explicit `approve` / `deny` responses.
- macOS privacy prompts still require local OS-level approval.

If you make this public-facing or multi-user, add authentication, authorization, audit logs, and stricter approval policies before using it for sensitive machines.

## Development

Install dependencies:

```bash
UV_CACHE_DIR=.uv-cache uv sync --extra feishu --extra test
```

Run tests:

```bash
UV_CACHE_DIR=.uv-cache uv run pytest
UV_CACHE_DIR=.uv-cache uv run python -m compileall -q src tests
```

Run HTTP server:

```bash
UV_CACHE_DIR=.uv-cache uv run feishu-codex-bridge
```

Run Feishu websocket bridge:

```bash
BRIDGE_CODEX_USE_APP_SERVER=true \
BRIDGE_DRY_RUN_REPLIES=false \
UV_CACHE_DIR=.uv-cache uv run feishu-codex-bridge feishu-ws -v
```

## Troubleshooting

### Feishu receives nothing

- Confirm only one websocket client is connected for the same Feishu app.
- If Hermes is using Feishu websocket mode, stop Hermes first.
- Keep `BRIDGE_DRY_RUN_REPLIES=false` for real replies.

### Replies appear as threaded replies instead of normal messages

Set:

```bash
BRIDGE_FEISHU_DELIVERY_MODE=send
```

If Feishu rejects `chat_id` sending, the bridge falls back to message replies automatically.

### Second message hangs or crashes

Large Codex app-server events can exceed small stream limits. The default is already raised:

```bash
BRIDGE_CODEX_STREAM_LIMIT_BYTES=16777216
```

### Images do not upload back to Feishu

- Make sure Codex returns an absolute local file path.
- Make sure the file exists and is below `BRIDGE_MAX_MEDIA_BYTES`.
- Check that the Feishu app has image/file upload permissions.

## Roadmap

- Multi-account and multi-tenant authorization.
- Better audit trail for approvals.
- Rich Feishu cards for approval prompts.
- Admin UI for conversation/thread mappings.
- Deployment templates for launchd/systemd.
- Optional queueing for long-running Codex tasks.

## License

No license has been selected yet. Add one before public distribution.
