"""
monobank acquiring (plata by mono). Документація: https://api.monobank.ua/docs/acquiring.html

Два методи, які нам потрібні на цьому кроці:
  POST /api/merchant/invoice/create   → invoiceId + pageUrl (сторінка оплати)
  GET  /api/merchant/invoice/status   → status рахунку
Авторизація — заголовок X-Token. Тестовий токен (api.monobank.ua) працює з тим самим
кодом, просто гроші не списуються. Тому код однаковий для демо і для реального клієнта.

Суми — у КОПІЙКАХ (490 грн = 49000), валюта 980 = гривня (ISO 4217).
"""
import logging
import ssl

import aiohttp
import certifi

from bot.payments import Invoice, PaymentError

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


class MonoProvider:
    def __init__(self, token: str, base_url: str = MONO_API, timeout: float = 15) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

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
                             redirect_url: str | None, validity_seconds: int) -> Invoice:
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
