# Feishu Codex Bridge

Standalone bridge for:

```text
Feishu/Lark message → local bridge → Codex → Feishu reply
```

The project has two Codex modes:

- `codex exec` text mode, working now for smoke tests.
- Codex app-server mode, working as the real Codex app/thread bridge.

## Quick Start

```bash
cd feishu-codex-bridge
cp .env.example .env
UV_CACHE_DIR=.uv-cache uv sync --extra feishu --extra test
UV_CACHE_DIR=.uv-cache uv run feishu-codex-bridge
```

Open:

```bash
curl http://127.0.0.1:8788/health
```

## Local Simulation

This tests Feishu → bridge → Codex without using Feishu:

```bash
curl -X POST http://127.0.0.1:8788/simulate \
  -H 'content-type: application/json' \
  -d '{"text":"Reply with exactly: Feishu Codex bridge OK"}'
```

## Codex App Thread Mode

To make Feishu conversations appear in the Codex interface:

```bash
BRIDGE_CODEX_USE_APP_SERVER=true UV_CACHE_DIR=.uv-cache uv run feishu-codex-bridge
```

Then simulate:

```bash
curl -X POST http://127.0.0.1:8788/simulate \
  -H 'content-type: application/json' \
  -d '{"conversation_id":"feishu-chat-1","text":"Reply with exactly: Codex app thread OK"}'
```

Behavior:

- The bridge creates a non-ephemeral Codex thread.
- The thread is named `Feishu: <conversation_id>`.
- The mapping is stored in `.codex-thread-map.json`.
- Later messages with the same `conversation_id` resume the same Codex thread.
- The Codex UI can show that thread/history.

## Feishu Setup

1. Create or reuse a Feishu/Lark custom app.
2. Enable bot capability.
3. Enable message event subscription.
4. Point Feishu event callback to:

   `https://YOUR_PUBLIC_URL/feishu/events`

5. Set `.env`:

   - `BRIDGE_FEISHU_APP_ID`
   - `BRIDGE_FEISHU_APP_SECRET`
   - `BRIDGE_FEISHU_VERIFICATION_TOKEN`
   - `BRIDGE_FEISHU_ENCRYPT_KEY` if Feishu encryption is enabled
   - `BRIDGE_DRY_RUN_REPLIES=false` after local tests pass

For local development, expose the bridge with Cloudflare Tunnel or ngrok.

## Hermes Feishu Websocket Mode

If Hermes is already configured for Feishu long-connection mode, this bridge can reuse the same app credentials from `~/.hermes/.env`.

Install websocket support:

```bash
cd feishu-codex-bridge
UV_CACHE_DIR=.uv-cache uv sync --extra feishu --extra test
```

Run the standalone Feishu websocket bridge:

```bash
BRIDGE_CODEX_USE_APP_SERVER=true \
BRIDGE_DRY_RUN_REPLIES=false \
UV_CACHE_DIR=.uv-cache uv run feishu-codex-bridge feishu-ws -v
```

Important:

- Stop the Hermes gateway first if it is using `FEISHU_CONNECTION_MODE=websocket`; only one client should hold the Feishu websocket connection for the same app.
- `BRIDGE_FEISHU_DELIVERY_MODE=send` posts normal visible chat messages. Use `reply` only if you want Feishu threaded replies under each user message.
- `BRIDGE_FEISHU_APPROVAL_ENABLED=true` forwards Codex approval prompts to Feishu. Reply `approve` or `deny`.
- The default Codex model is `gpt-5.5` with `BRIDGE_CODEX_REASONING_EFFORT=medium`; image fallback inherits those settings unless `BRIDGE_IMAGE_CODEX_MODEL` or `BRIDGE_IMAGE_CODEX_REASONING_EFFORT` is set.
- Feishu `chat_id` to Codex `thread_id` mapping is stored in the bridge project at `.codex-thread-map.json`, so restarting from another shell directory still resumes the same Codex conversation.
- `BRIDGE_CODEX_STREAM_LIMIT_BYTES` defaults to 16MB to handle large app-server events from tool use, screenshots, and long context without crashing the second turn.
- Keep `BRIDGE_DRY_RUN_REPLIES=true` for the first live connection test if you only want logs and Codex processing without Feishu replies.
- The bridge maps each Feishu `chat_id` to a persistent Codex thread, so follow-up messages from the same chat continue in the same Codex UI conversation.
- Hermes env fallback reads `FEISHU_APP_ID`, `FEISHU_APP_SECRET`, `FEISHU_VERIFICATION_TOKEN`, `FEISHU_ENCRYPT_KEY`, and `FEISHU_DOMAIN`.
- Text, image, and file messages are supported. Images/files are downloaded into `feishu-media/` and passed to Codex as local file paths.
- Image messages are saved as real `.jpg`/`.png` files. By default, image analysis uses `codex exec --image` because some app-server model sessions reject image inputs.

## Codex App Features and Safety

App-server mode uses JSON-RPC methods including `thread/start`, `thread/resume`, `thread/name/set`, and `turn/start`.

Codex loads its configured tools/plugins for the thread, including local MCP servers where available. The bridge intentionally **does not auto-approve dangerous local actions** from Feishu.

When Feishu approval forwarding is enabled, the bridge sends a one-time approval code into the same Feishu chat and waits for `approve <code>` or `deny <code>`. This makes Feishu the remote approval channel without granting blanket Computer Use access.

## Safety Defaults

- `BRIDGE_DRY_RUN_REPLIES=true` prevents accidental Feishu posting.
- `codex exec` runs with `--sandbox read-only`.
- App-server mode creates persistent Codex threads, but does not auto-approve command/file/computer-use actions.
