"""
Клієнт mono проти ЛОКАЛЬНОГО сервера, що прикидається mono (aiohttp.test_utils).

Перевіряємо дві речі:
1) ЩО ми надсилаємо: токен у заголовку, сума в копійках, валюта 980, наш reference;
2) ЯК реагуємо на погане: 403, 429, 500, не-JSON, таймаут — завжди PaymentError,
   а не падіння бота з незрозумілою помилкою.
Справжній mono перевіримо живим тестом на Mac.
"""
import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bot.payments import PaymentError
from bot.payments.mono import MonoProvider


def serve(handler_create=None, handler_status=None):
    """Запускає фальшивий mono, віддає MonoProvider, що дивиться на нього, і журнал запитів."""
    seen: list[dict] = []

    async def create(request: web.Request):
        seen.append({"token": request.headers.get("X-Token"), "body": await request.json()})
        if handler_create:
            return await handler_create(request)
        return web.json_response({"invoiceId": "abc123", "pageUrl": "https://pay.mbnk.biz/abc123"})

    async def status(request: web.Request):
        seen.append({"token": request.headers.get("X-Token"), "query": dict(request.query)})
        if handler_status:
            return await handler_status(request)
        return web.json_response({"invoiceId": request.query["invoiceId"], "status": "success"})

    app = web.Application()
    app.router.add_post("/api/merchant/invoice/create", create)
    app.router.add_get("/api/merchant/invoice/status", status)
    return app, seen


def run(app, coro_fn, timeout: float = 5):
    async def go():
        server = TestServer(app)
        await server.start_server()
        provider = MonoProvider("TEST-TOKEN", base_url=str(server.make_url("")), timeout=timeout)
        try:
            return await coro_fn(provider)
        finally:
            await provider.close()
            await server.close()
    return asyncio.run(go())


async def _create(p: MonoProvider):
    return await p.create_invoice(amount_kop=49000, reference="payment-7", description="Сила вдома — 1 місяць",
                                  redirect_url="https://t.me/test_bot", validity_seconds=1800)


def test_create_invoice_request_and_response():
    app, seen = serve()
    invoice = run(app, _create)
    assert invoice.invoice_id == "abc123" and invoice.page_url.startswith("https://pay.mbnk.biz/")
    sent = seen[0]
    assert sent["token"] == "TEST-TOKEN"
    assert sent["body"]["amount"] == 49000 and sent["body"]["ccy"] == 980
    assert sent["body"]["merchantPaymInfo"] == {"reference": "payment-7", "destination": "Сила вдома — 1 місяць"}
    assert sent["body"]["redirectUrl"] == "https://t.me/test_bot" and sent["body"]["validity"] == 1800


@pytest.mark.parametrize("mono_status, ours", [
    ("created", "created"), ("processing", "processing"), ("hold", "processing"),
    ("success", "success"), ("failure", "failure"), ("expired", "expired"),
])
def test_status_mapping(mono_status, ours):
    async def status(request):
        return web.json_response({"status": mono_status})
    app, seen = serve(handler_status=status)
    assert run(app, lambda p: p.get_status("abc123")) == ours
    assert seen[0]["query"] == {"invoiceId": "abc123"}


def test_unknown_status_is_an_error_not_silence():
    """Напр. reversed (повернення коштів) — не вдаємо, що все гаразд."""
    async def status(request):
        return web.json_response({"status": "reversed"})
    app, _ = serve(handler_status=status)
    with pytest.raises(PaymentError, match="невідомий статус"):
        run(app, lambda p: p.get_status("abc123"))


@pytest.mark.parametrize("http_status, body, message", [
    (403, {"errCode": "FORBIDDEN", "errText": "forbidden"}, "недійсний токен"),
    (429, {"errText": "too many requests"}, "забагато запитів"),
    (400, {"errCode": "BAD_REQUEST", "errText": "invalid 'amount'"}, "invalid 'amount'"),
    (500, {}, "mono 500"),
])
def test_bank_errors_become_payment_error(http_status, body, message):
    async def create(request):
        return web.json_response(body, status=http_status)
    app, _ = serve(handler_create=create)
    with pytest.raises(PaymentError, match=message):
        run(app, _create)


def test_html_instead_of_json():
    async def create(request):
        return web.Response(text="<html>502 Bad Gateway</html>", status=502, content_type="text/html")
    app, _ = serve(handler_create=create)
    with pytest.raises(PaymentError):
        run(app, _create)


def test_timeout():
    async def create(request):
        await asyncio.sleep(2)
        return web.json_response({})
    app, _ = serve(handler_create=create)
    with pytest.raises(PaymentError, match="недоступний"):
        run(app, _create, timeout=0.3)


def test_success_without_invoice_id_is_error():
    async def create(request):
        return web.json_response({"pageUrl": "https://x"})
    app, _ = serve(handler_create=create)
    with pytest.raises(PaymentError, match="invoiceId"):
        run(app, _create)
