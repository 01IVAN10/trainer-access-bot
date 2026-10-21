"""
Webhook від mono — найчутливіше місце бота: сюди може постукати будь-хто з інтернету.

Справжній підпис mono ми згенерувати не можемо (у нас немає його закритого ключа).
Тому в тесті ми САМІ стаємо «mono»: генеруємо пару ключів, віддаємо відкритий ключ
через фальшивий mono-сервер і підписуємо webhook закритим. Код бота при цьому
справжній — той самий MonoProvider, що працює з api.monobank.ua.
"""
import asyncio
import base64
import json
from datetime import timedelta

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from bot import db
from bot.payments.mono import PUBKEY_REFRESH_INTERVAL, MonoProvider
from bot.plans import load_plans
from bot.subscription import utcnow
from bot.webhook import WEBHOOK_PATH, build_web_app
from tests.fake_telegram import USER, make_bot, make_config

PLANS = load_plans()


def new_key():
    return ec.generate_private_key(ec.SECP256R1())


def pem_b64(private_key) -> str:
    pem = private_key.public_key().public_bytes(serialization.Encoding.PEM,
                                                serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(pem).decode()  # як віддає mono: PEM, закодований у base64


def sign(private_key, body: bytes) -> str:
    return base64.b64encode(private_key.sign(body, ec.ECDSA(hashes.SHA256()))).decode()


def event(status="success", invoice_id="inv1", amount=49000) -> bytes:
    return json.dumps({"invoiceId": invoice_id, "status": status, "amount": amount, "ccy": 980,
                       "reference": "payment-1", "modifiedDate": "2026-10-21T16:00:00Z"}).encode()


class World:
    """Фальшивий mono (лише /pubkey) + наш справжній HTTP-сервер + база з одним платежем."""

    def __init__(self) -> None:
        self.mono_key = new_key()
        self.pubkey_requests = 0
        self.mono_down = False

    async def _pubkey(self, request):
        self.pubkey_requests += 1
        if self.mono_down:
            return web.json_response({}, status=500)
        return web.json_response({"key": pem_b64(self.mono_key)})

    def run(self, script):
        async def go():
            await db.init(":memory:")
            mono_app = web.Application()
            mono_app.router.add_get("/api/merchant/pubkey", self._pubkey)
            mono = TestServer(mono_app)
            await mono.start_server()
            self.provider = MonoProvider("T", base_url=str(mono.make_url("")))
            self.bot, self.session = make_bot()
            client = TestClient(TestServer(build_web_app(self.bot, self.provider, PLANS, make_config())))
            await client.start_server()
            try:
                await db.upsert_user(USER.id, USER.username, USER.first_name)
                pid = await db.create_payment(USER.id, PLANS["month"], utcnow())
                await db.set_invoice(pid, "inv1", "https://pay.test/inv1", utcnow() + timedelta(minutes=30))
                return await script(client)
            finally:
                await client.close()
                await self.provider.close()
                await mono.close()
                await db.close()
        return asyncio.run(go())

    def post(self, client, body: bytes, key=None, signature: str | None = "auto"):
        headers = {}
        if signature == "auto":
            headers["X-Sign"] = sign(key or self.mono_key, body)
        elif signature is not None:
            headers["X-Sign"] = signature
        return client.post(WEBHOOK_PATH, data=body, headers=headers)


def test_valid_success_webhook_gives_access_and_notifies():
    w = World()

    async def script(client):
        resp = await w.post(client, event("success"))
        return resp.status, await db.get_subscription(USER.id), await db.get_payment(1)

    status, sub, payment = w.run(script)
    assert status == 200 and payment["status"] == "success" and sub is not None
    sent = w.session.sent()
    assert len(sent) == 1 and sent[0].chat_id == USER.id and "Оплату отримано" in sent[0].text


def test_same_webhook_three_times_counts_once():
    """mono може повторити webhook (до 3 разів) — продовження і повідомлення лише одне."""
    w = World()

    async def script(client):
        for _ in range(3):
            assert (await w.post(client, event("success"))).status == 200
        return await db.get_subscription(USER.id)

    sub = w.run(script)
    assert sub.expires_at - utcnow() <= timedelta(days=30)
    assert len(w.session.sent()) == 1


def test_forged_signature_rejected_and_nothing_changes():
    """Хтось знає адресу webhook і шле «оплачено», підписане СВОЇМ ключем."""
    w = World()
    attacker = new_key()

    async def script(client):
        resp = await w.post(client, event("success"), key=attacker)
        return resp.status, await db.get_subscription(USER.id), await db.get_payment(1)

    status, sub, payment = w.run(script)
    assert status == 403 and sub is None and payment["status"] == "created"
    assert w.session.sent() == []


def test_missing_or_garbage_signature():
    w = World()

    async def script(client):
        a = (await w.post(client, event(), signature=None)).status
        b = (await w.post(client, event(), signature="не-base64!!!")).status
        return a, b, await db.get_subscription(USER.id)

    a, b, sub = w.run(script)
    assert a == b == 403 and sub is None


def test_tampered_body_rejected():
    """Справжній підпис, але тіло змінили (напр. статус) — підпис більше не збігається."""
    w = World()

    async def script(client):
        original = event("processing")
        signature = sign(w.mono_key, original)
        tampered = original.replace(b'"processing"', b'"success"')
        resp = await w.post(client, tampered, signature=signature)
        return resp.status, await db.get_subscription(USER.id)

    status, sub = w.run(script)
    assert status == 403 and sub is None


def test_forged_flood_does_not_spam_mono_for_keys():
    """100 фальшивих webhook-ів — а ключ у mono ми запитали лише раз (інакше mono дав би нам 429)."""
    w = World()
    attacker = new_key()

    async def script(client):
        for _ in range(100):
            await w.post(client, event(), key=attacker)

    w.run(script)
    assert w.pubkey_requests == 1


def test_mono_rotated_key():
    """mono змінив ключ: старий у нас у пам'яті не підходить → перезапитуємо (але не частіше, ніж раз на 10 хв)."""
    w = World()

    async def script(client):
        assert (await w.post(client, event("processing"))).status == 200   # ключ A отримано
        w.mono_key = new_key()                                              # mono перейшов на ключ B
        recent = (await w.post(client, event("success"))).status            # щойно брали ключ → не смикаємо
        w.provider._pubkey_fetched_at -= PUBKEY_REFRESH_INTERVAL + 1        # «минуло 10 хвилин»
        later = (await w.post(client, event("success"))).status             # перезапит → ключ B → ок
        return recent, later, await db.get_subscription(USER.id)

    recent, later, sub = w.run(script)
    assert recent == 403 and later == 200 and sub is not None
    assert w.pubkey_requests == 2


def test_out_of_order_processing_after_success():
    """Порядок не гарантований: «processing» прийшов ПІСЛЯ «success». Нічого не зламалось."""
    w = World()

    async def script(client):
        await w.post(client, event("success"))
        await w.post(client, event("processing"))
        return await db.get_payment(1), await db.get_subscription(USER.id)

    payment, sub = w.run(script)
    assert payment["status"] == "success" and sub is not None


def test_failure_webhook():
    w = World()

    async def script(client):
        await w.post(client, event("failure"))
        return await db.get_payment(1), await db.get_subscription(USER.id)

    payment, sub = w.run(script)
    assert payment["status"] == "failure" and sub is None and w.session.sent() == []


def test_unknown_invoice_is_ignored_with_200():
    """Рахунок не наш (tools/mono_try, інше оточення) — 200, щоб mono не повторював, і нічого не робимо."""
    w = World()

    async def script(client):
        resp = await w.post(client, event("success", invoice_id="someone-else"))
        return resp.status, await db.get_subscription(USER.id)

    status, sub = w.run(script)
    assert status == 200 and sub is None


def test_amount_mismatch_not_applied():
    w = World()

    async def script(client):
        resp = await w.post(client, event("success", amount=100))  # 1 грн замість 490
        return resp.status, await db.get_subscription(USER.id)

    status, sub = w.run(script)
    assert status == 200 and sub is None


def test_unknown_status_like_reversed_is_ignored():
    w = World()

    async def script(client):
        resp = await w.post(client, event("reversed"))
        return resp.status, await db.get_payment(1)

    status, payment = w.run(script)
    assert status == 200 and payment["status"] == "created"


def test_mono_down_while_fetching_key_asks_to_retry():
    """Не змогли дістати ключ → 503: mono повторить пізніше, а не вважатиме, що ми прийняли."""
    w = World()
    w.mono_down = True

    async def script(client):
        return (await w.post(client, event("success"))).status

    assert w.run(script) == 503


def test_health():
    w = World()

    async def script(client):
        resp = await client.get("/")
        return resp.status, await resp.text()

    assert w.run(script) == (200, "ok")
