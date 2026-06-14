from types import SimpleNamespace

from feishu_codex_bridge.feishu_events import challenge_response, parse_feishu_event, parse_lark_message_event


def test_challenge_response() -> None:
    assert challenge_response({"type": "url_verification", "challenge": "abc"}) == {
        "challenge": "abc"
    }


def test_parse_text_message_event() -> None:
    payload = {
        "header": {"event_id": "evt-1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}},
            "message": {
                "message_id": "om_1",
                "chat_id": "oc_1",
                "message_type": "text",
                "content": "{\"text\":\"  hello   codex  \"}",
            },
        },
    }

    message = parse_feishu_event(payload)

    assert message is not None
    assert message.event_id == "evt-1"
    assert message.message_id == "om_1"
    assert message.chat_id == "oc_1"
    assert message.sender_id == "ou_1"
    assert message.text == "hello codex"


def test_parse_ignores_unsupported_type() -> None:
    payload = {"event": {"message": {"message_type": "audio"}}}

    assert parse_feishu_event(payload) is None


def test_parse_image_and_file_events() -> None:
    image_payload = {
        "header": {"event_id": "evt-image"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}},
            "message": {
                "message_id": "om_image",
                "chat_id": "oc_1",
                "message_type": "image",
                "content": "{\"image_key\":\"img_key\"}",
            },
        },
    }
    file_payload = {
        "header": {"event_id": "evt-file"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_1"}},
            "message": {
                "message_id": "om_file",
                "chat_id": "oc_1",
                "message_type": "file",
                "content": "{\"file_key\":\"file_key\",\"file_name\":\"notes.pdf\"}",
            },
        },
    }

    image = parse_feishu_event(image_payload)
    file = parse_feishu_event(file_payload)

    assert image is not None
    assert image.message_type == "image"
    assert image.content["image_key"] == "img_key"
    assert file is not None
    assert file.message_type == "file"
    assert file.content["file_key"] == "file_key"
    assert file.content["file_name"] == "notes.pdf"


def test_parse_lark_message_event() -> None:
    data = SimpleNamespace(
        header=SimpleNamespace(event_id="evt-2"),
        event=SimpleNamespace(
            sender=SimpleNamespace(sender_id=SimpleNamespace(open_id="ou_2")),
            message=SimpleNamespace(
                message_id="om_2",
                chat_id="oc_2",
                message_type=SimpleNamespace(value="text"),
                content="{\"text\":\" hi from websocket \"}",
            ),
        ),
    )

    message = parse_lark_message_event(data)

    assert message is not None
    assert message.event_id == "evt-2"
    assert message.message_id == "om_2"
    assert message.chat_id == "oc_2"
    assert message.sender_id == "ou_2"
    assert message.text == "hi from websocket"
