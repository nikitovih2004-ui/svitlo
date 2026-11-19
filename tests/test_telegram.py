import asyncio
import json

import httpx
import pytest

from svitlo.telegram import NetworkError, Telegram, TelegramError

TOKEN = "123456:SECRET-TOKEN"


def make(handler, retries=3):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Telegram(TOKEN, client=client, retries=retries, sleep=fake_sleep), sleeps


def ok(result):
    return httpx.Response(200, json={"ok": True, "result": result})


def err(code, desc, **params):
    body = {"ok": False, "error_code": code, "description": desc}
    if params:
        body["parameters"] = params
    return httpx.Response(code, json=body)


def test_send_widget_payload():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return ok({"message_id": 42})

    tg, _ = make(handler)
    msg = asyncio.run(tg.send_widget(7, "<p>x</p>", chat_data='{"r":1}', caption="Свет дали"))
    assert msg["message_id"] == 42
    assert seen["url"].endswith("/bot%s/sendWidget" % TOKEN)
    assert seen["body"] == {"chat_id": 7, "html": "<p>x</p>", "chat_data": '{"r":1}', "caption": "Свет дали"}


def test_send_widget_optional_flags():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return ok({"message_id": 1})

    tg, _ = make(handler)
    asyncio.run(tg.send_widget(7, "<p/>", width=320, height=240, allow_queries=True, disable_notification=True))
    assert seen["width"] == 320 and seen["height"] == 240
    assert seen["allow_queries"] is True and seen["disable_notification"] is True


def test_edit_not_modified_is_success():
    tg, _ = make(lambda r: err(400, "Bad Request: message is not modified"))
    assert asyncio.run(tg.edit_widget_data(7, 1, "{}")) is True


def test_429_waits_retry_after():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return err(429, "Too Many Requests: retry after 7", retry_after=7)
        return ok(True)

    tg, sleeps = make(handler)
    assert asyncio.run(tg.edit_widget_data(7, 1, "{}")) is True
    assert sleeps == [7.0] and len(calls) == 2


def test_5xx_and_network_retries():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("boom")
        if len(calls) == 2:
            return httpx.Response(502, text="Bad Gateway")
        return ok({"message_id": 5})

    tg, sleeps = make(handler)
    assert asyncio.run(tg.edit_widget_data(7, 1, "{}")) is True
    assert len(sleeps) == 2


def test_send_retried_only_when_surely_not_delivered():
    """sendMessage/sendWidget: ошибка соединения — повтор; таймаут ответа или 5xx — без повтора
    (сообщение могло уйти, повтор решает сверка в bot.py)."""
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("boom")
        return ok({"message_id": 5})

    tg, sleeps = make(handler)
    assert asyncio.run(tg.send_message(7, "hi"))["message_id"] == 5 and len(calls) == 2

    for exc_or_resp in (httpx.ReadTimeout("slow"), httpx.Response(502, text="Bad Gateway")):
        calls.clear()

        def handler2(request, x=exc_or_resp):
            calls.append(1)
            if isinstance(x, Exception):
                raise x
            return x

        tg, _ = make(handler2)
        with pytest.raises((NetworkError, TelegramError)):
            asyncio.run(tg.send_widget(7, "<p/>"))
        assert len(calls) == 1


def test_delete_message():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return ok(True)

    tg, _ = make(handler)
    assert asyncio.run(tg.delete_message(7, 42)) is True
    assert seen["path"].endswith("/deleteMessage") and seen["body"] == {"chat_id": 7, "message_id": 42}


def test_network_error_gives_up_without_token():
    def handler(request):
        raise httpx.ConnectError("boom %s" % TOKEN)

    tg, sleeps = make(handler, retries=2)
    with pytest.raises(NetworkError) as ei:
        asyncio.run(tg.edit_widget_data(7, 1, "{}"))
    assert TOKEN not in str(ei.value)
    assert len(sleeps) == 2


def test_api_error_raised():
    tg, _ = make(lambda r: err(400, "Bad Request: INPUT_MEDIA_WIDGET_TOO_LARGE"))
    with pytest.raises(TelegramError) as ei:
        asyncio.run(tg.send_widget(7, "<p/>"))
    assert ei.value.code == 400 and "TOO_LARGE" in ei.value.description
    assert TOKEN not in str(ei.value)


def test_error_classification():
    assert TelegramError("m", 403, "Forbidden: bot was blocked by the user").blocked
    assert TelegramError("m", 400, "Bad Request: message to edit not found").gone
    assert not TelegramError("m", 400, "Bad Request: message is not modified").gone


def test_get_updates_and_answer_query():
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append((request.url.path.rsplit("/", 1)[-1], body))
        if request.url.path.endswith("getUpdates"):
            return ok([{"update_id": 10}])
        return ok(True)

    tg, _ = make(handler)
    ups = asyncio.run(tg.get_updates(5, timeout=25, allowed_updates=["message", "widget_query"]))
    assert ups == [{"update_id": 10}]
    asyncio.run(tg.answer_widget_query("q1", '{"r":1}'))
    assert seen[0] == ("getUpdates", {"offset": 5, "timeout": 25, "allowed_updates": ["message", "widget_query"]})
    assert seen[1] == ("answerWidgetQuery", {"widget_query_id": "q1", "data": '{"r":1}'})


def test_test_env_url():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        return ok(True)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tg = Telegram(TOKEN, client=client, test_env=True)
    asyncio.run(tg.call("getMe"))
    assert seen["path"] == "/bot%s/test/getMe" % TOKEN
