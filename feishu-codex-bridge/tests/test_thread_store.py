from feishu_codex_bridge.thread_store import ThreadStore


def test_thread_store_round_trip(tmp_path) -> None:
    store = ThreadStore(tmp_path / "threads.json")

    assert store.get("chat") is None
    store.set("chat", "thread-1")
    assert store.get("chat") == "thread-1"
    store.delete("chat")
    assert store.get("chat") is None


def test_thread_store_ignores_invalid_json(tmp_path) -> None:
    path = tmp_path / "threads.json"
    path.write_text("{not-json", encoding="utf-8")

    assert ThreadStore(path).get("chat") is None
