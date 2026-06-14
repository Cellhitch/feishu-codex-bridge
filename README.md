# Feishu Codex Bridge

Remote-control the local Codex app from Feishu or Lark.

This repository contains a standalone Python bridge that connects Feishu/Lark chat messages to the local Codex app. It is built for a simple idea:

> Send a message from Feishu, let Codex work on the Mac, and receive the answer or generated files back in Feishu.

## What It Does

- Receives Feishu/Lark text, image, and file messages.
- Sends each chat into a persistent local Codex thread.
- Shows remote conversations in the Codex app UI.
- Sends Codex replies, screenshots, images, and files back to Feishu.
- Forwards local approval prompts to Feishu with `approve` / `deny`.
- Keeps local secrets, logs, media, and thread maps out of Git.

## Project

The bridge lives in:

```text
feishu-codex-bridge/
```

Start there:

```bash
cd feishu-codex-bridge
cp .env.example .env
UV_CACHE_DIR=.uv-cache uv sync --extra feishu --extra test
```

Recommended run mode:

```bash
BRIDGE_CODEX_USE_APP_SERVER=true \
BRIDGE_DRY_RUN_REPLIES=false \
UV_CACHE_DIR=.uv-cache uv run feishu-codex-bridge feishu-ws -v
```

## Documentation

Read the full guide:

```text
feishu-codex-bridge/README.md
```

## Safety

This project is local-first. It does not publish your Codex environment as a public API. Local media, `.env` files, logs, and Codex thread maps are ignored by Git.

If you plan to make it public or support many users, add authentication, authorization, audit logging, and stronger approval controls first.
