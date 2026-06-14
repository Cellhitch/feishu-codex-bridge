from __future__ import annotations

import argparse
import asyncio
import logging

import uvicorn

from .app import create_app
from .approval import FeishuApprovalCoordinator
from .codex_client import create_codex_client
from .config import Settings
from .feishu_client import FeishuClient
from .feishu_ws import FeishuWebsocketBridge


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Feishu Codex Bridge.")
    parser.add_argument(
        "mode",
        nargs="?",
        choices=["server", "feishu-ws"],
        default="server",
        help="server runs HTTP webhook/simulate API; feishu-ws connects to Feishu long-connection mode.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    settings = Settings()
    if args.mode == "feishu-ws":
        feishu = FeishuClient(settings)
        approvals = FeishuApprovalCoordinator(settings, feishu)
        codex = create_codex_client(settings, approval_handler=approvals.request_approval)
        bridge = FeishuWebsocketBridge(settings, codex, feishu, approvals)
        asyncio.run(bridge.run_forever())
        return
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
