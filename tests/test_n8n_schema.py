from __future__ import annotations

from datetime import datetime, timezone

from app.bot.schemas.n8n_io import ChatInfo, Context, HistoryItem, Meta, N8nRequest, N8nResponse


def test_n8n_request_response_models():
    history = [
        HistoryItem(role="user", text="hi", created_at=datetime.now(timezone.utc)),
        HistoryItem(role="assistant", text="hello", created_at=datetime.now(timezone.utc)),
    ]
    ctx = Context(history=history)
    chat = ChatInfo(chat_id=123, user_id=1, lang="ru", username="test")
    req = N8nRequest(intent="reply", chat=chat, context=ctx, message=None, trace_id="abc")

    # Validate dump
    payload = req.model_dump()
    assert payload["intent"] == "reply"
    assert payload["chat"]["chat_id"] == 123
    assert len(payload["context"]["history"]) == 2

    # Response
    meta = Meta(model="gpt", tokens=42, extra="ok")
    resp = N8nResponse(reply="pong", meta=meta)
    assert resp.reply == "pong"
    assert resp.meta.model == "gpt"
    assert resp.meta.tokens == 42


def test_message_in_allows_image_fields():
    from app.bot.schemas.n8n_io import MessageIn
    m = MessageIn(text="[photo]", origin="photo", image_url="https://x/y.jpg", width=800, height=600)
    d = m.model_dump()
    assert d["origin"] == "photo"
    assert d["image_url"].endswith(".jpg")

