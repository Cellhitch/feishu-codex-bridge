from __future__ import annotations

import json
from pathlib import Path


class ThreadStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def get(self, conversation_id: str) -> str | None:
        return self._load().get(conversation_id)

    def set(self, conversation_id: str, thread_id: str) -> None:
        data = self._load()
        data[conversation_id] = thread_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

    def delete(self, conversation_id: str) -> None:
        data = self._load()
        if conversation_id in data:
            del data[conversation_id]
            self.path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return {str(key): str(value) for key, value in data.items()}
