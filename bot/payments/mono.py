"""
monobank acquiring (plata by mono). Документація: https://api.monobank.ua/docs/acquiring.html

Два методи, які нам потрібні на цьому кроці:
  POST /api/merchant/invoice/create   → invoiceId + pageUrl (сторінка оплати)
  GET  /api/merchant/invoice/status   → status рахунку
Авторизація — заголовок X-Token. Тестовий токен (api.monobank.ua) працює з тим самим
кодом, просто гроші не списуються. Тому код однаковий для демо і для реального клієнта.

Суми — у КОПІЙКАХ (490 грн = 49000), валюта 980 = гривня (ISO 4217).

WEBHOOK (крок 4). mono сам надсилає POST на наш webHookUrl при кожній зміні статусу
(крім expired), до 3 спроб, порядок НЕ гарантований. До кожного — заголовок X-Sign:
підпис тіла запиту закритим ключем mono (ECDSA + SHA-256). Відкритий ключ беремо в mono
(GET /api/merchant/pubkey). Підробити підпис без закритого ключа неможливо — тому
«оплачено» на нашу адресу може надіслати хто завгодно, але ПРИЙМЕМО лише від mono.
"""
import base64
import json
import logging
import ssl
import time

import aiohttp
import certifi
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from bot.payments import Invoice, PaymentError, WebhookEvent, WebhookRejected

log = logging.getLogger(__name__)

MONO_API = "https://api.monobank.ua"
UAH = 980

# Сертифікати з certifi, а не системні: Python з python.org на macOS не бачить системних
# (див. крок 0 — ClientConnectorCertificateError). На Railway працює так само.
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

# Статуси mono → наші. hold (кошти заблоковано, двоетапна оплата) для нас = ще в процесі.
_STATUS_MAP = {
    "created": "created",
    "processing": "processing",
    "hold": "processing",
    "success": "success",
    "failure": "failure",
    "expired": "expired",
}


# Не перезапитувати ключ частіше, ніж раз на 10 хв (див. verify_signature).
PUBKEY_REFRESH_INTERVAL = 600


class MonoProvider:
    def __init__(self, token: str, base_url: str = MONO_API, timeout: float = 15) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None
        self._pubkey: ec.EllipticCurvePublicKey | None = None
        self._pubkey_fetched_at = float("-inf")

    def _get_session(self) -> aiohttp.ClientSession:
        # Одна сесія на весь бот (повторно використовує з'єднання), створюється при першому запиті.
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout, trust_env=True,
                connector=aiohttp.TCPConnector(ssl=SSL_CONTEXT),
                headers={"X-Token": self._token},
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        """Один запит до mono. БУДЬ-ЯКА проблема → PaymentError з поясненням для логів.

        Хендлеру не треба знати про 403, 429, таймаути й обірваний інтернет —
        йому достатньо «не вийшло, скажи людині спробувати пізніше».
        """
        url = f"{self._base_url}{path}"
        try:
            async with self._get_session().request(method, url, **kwargs) as resp:
                data = await resp.json(content_type=None)  # mono інколи не ставить JSON-заголовок
                if resp.status == 200:
                    return data or {}
                # Формат помилок mono: {"errCode": "...", "errText": "..."}
                err = (data or {}).get("errText") or (data or {}).get("errCode") or "без опису"
                if resp.status in (401, 403):
                    raise PaymentError(f"mono {resp.status}: недійсний токен ({err})")
                if resp.status == 429:
                    raise PaymentError("mono 429: забагато запитів, треба зачекати")
                raise PaymentError(f"mono {resp.status}: {err}")
        except PaymentError:
            raise
        except (aiohttp.ClientError, TimeoutError, ValueError) as e:
            # ValueError — коли у відповіді не JSON (наприклад, HTML-сторінка помилки)
            raise PaymentError(f"mono недоступний: {type(e).__name__}: {e}") from e

    async def create_invoice(self, *, amount_kop: int, reference: str, description: str,
                             redirect_url: str | None, validity_seconds: int,
                             webhook_url: str | None = None) -> Invoice:
        body = {
            "amount": amount_kop,
            "ccy": UAH,
            "merchantPaymInfo": {
                "reference": reference,       # НАШ номер платежу — видно у виписці mono
                "destination": description,   # що бачить людина на сторінці оплати
            },
            "validity": validity_seconds,     # скільки живе рахунок
        }
        if redirect_url:
            body["redirectUrl"] = redirect_url  # куди повернути людину після оплати
        if webhook_url:
            body["webHookUrl"] = webhook_url    # куди mono надішле «статус змінився»
        data = await self._request("POST", "/api/merchant/invoice/create", json=body)
        if not data.get("invoiceId") or not data.get("pageUrl"):
            raise PaymentError(f"mono не повернув invoiceId/pageUrl: {data}")
        log.info("mono: рахунок %s на %s коп. (%s)", data["invoiceId"], amount_kop, reference)
        return Invoice(invoice_id=data["invoiceId"], page_url=data["pageUrl"])

    async def get_status(self, invoice_id: str) -> str:
        data = await self._request("GET", "/api/merchant/invoice/status",
                                   params={"invoiceId": invoice_id})
        raw = data.get("status")
        status = _STATUS_MAP.get(raw)
        if status is None:
            # Напр. reversed (повернення коштів) — поки не обробляємо, але й мовчки не ковтаємо.
            raise PaymentError(f"mono: невідомий статус {raw!r} для рахунку {invoice_id}")
        return status

    # ---------- webhook ----------

    async def _fetch_pubkey(self) -> None:
        data = await self._request("GET", "/api/merchant/pubkey")
        try:
            # mono віддає PEM-ключ, ще й закодований у base64: спершу base64 → PEM → ключ.
            key = serialization.load_pem_public_key(base64.b64decode(data["key"]))
        except (KeyError, ValueError, TypeError) as e:
            raise PaymentError(f"mono: не вдалося прочитати відкритий ключ: {e}") from e
        if not isinstance(key, ec.EllipticCurvePublicKey):
            raise PaymentError("mono: відкритий ключ не ECDSA")
        self._pubkey = key
        self._pubkey_fetched_at = time.monotonic()
        log.info("mono: відкритий ключ для перевірки webhook отримано")

    @staticmethod
    def _matches(key: ec.EllipticCurvePublicKey, body: bytes, signature: bytes) -> bool:
        try:
            key.verify(signature, body, ec.ECDSA(hashes.SHA256()))
            return True
        except InvalidSignature:
            return False

    async def verify_signature(self, body: bytes, x_sign: str | None) -> bool:
        """Чи підписано ЦІ САМІ байти тіла ключем mono.

        Саме «сирі» байти, а не розібраний JSON: якщо розібрати й зібрати JSON знову,
        пробіли чи порядок полів зміняться — і справжній підпис перестане збігатися.
        """
        if not x_sign:
            return False
        try:
            signature = base64.b64decode(x_sign, validate=True)
        except ValueError:
            return False
        if self._pubkey is None:
            await self._fetch_pubkey()  # перший webhook після старту — ключа ще немає
        if self._matches(self._pubkey, body, signature):
            return True
        # Не збіглося. Можливо, mono змінив ключ — тоді варто перезапитати (так радить документація).
        # Але НЕ щоразу: інакше будь-хто, засипаючи нас фальшивими webhook-ами, змусив би нас
        # засипати mono запитами за ключем — і mono заблокував би нас за 429.
        if time.monotonic() - self._pubkey_fetched_at < PUBKEY_REFRESH_INTERVAL:
            return False
        await self._fetch_pubkey()
        return self._matches(self._pubkey, body, signature)

    async def parse_webhook(self, body: bytes, headers) -> WebhookEvent:
        # 1) СПЕРШУ підпис, ПОТІМ усе інше. Непідписаним даним не віримо навіть у дрібницях.
        if not await self.verify_signature(body, headers.get("X-Sign")):
            raise WebhookRejected("підпис X-Sign не збігся")
        try:
            data = json.loads(body)
        except ValueError as e:
            raise WebhookRejected("тіло — не JSON") from e
        invoice_id, raw_status = data.get("invoiceId"), data.get("status")
        if not invoice_id or not raw_status:
            raise WebhookRejected("немає invoiceId або status")
        amount = data.get("amount")
        return WebhookEvent(
            invoice_id=invoice_id,
            status=_STATUS_MAP.get(raw_status),
            raw_status=raw_status,
            amount_kop=amount if isinstance(amount, int) and not isinstance(amount, bool) else None,
        )
