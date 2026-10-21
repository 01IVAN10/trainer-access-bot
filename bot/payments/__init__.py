"""
Платіжні провайдери. Решта бота знає лише ЦЕЙ інтерфейс, а не деталі mono.

Та сама ідея, що з ask_llm() у барбершопі: хендлер каже «створи рахунок на 49000 копійок»
і «який статус рахунку X», а як саме це робиться — справа провайдера. Тому:
- у тестах підставляємо фальшивий провайдер (tests/fake_payments.py) — без інтернету;
- Telegram Stars (крок 11) чи інший банк — ще один клас з тими самими методами.

Статуси — НАШІ (ті самі, що в таблиці payments), а не «мовою» конкретного банку:
  created     рахунок створено, людина ще не платила
  processing  оплата в процесі
  success     гроші отримано  ← ЛИШЕ тоді даємо доступ
  failure     оплата не пройшла
  expired     рахунок прострочено
"""
from dataclasses import dataclass
from typing import Protocol

STATUSES = ("created", "processing", "success", "failure", "expired")
FINAL_STATUSES = ("success", "failure", "expired")  # після них статус більше не змінюється


@dataclass(frozen=True)
class Invoice:
    invoice_id: str  # ID рахунку в банку — за ним питаємо статус
    page_url: str    # сторінка оплати, куди відправляємо людину


class PaymentError(Exception):
    """Банк недоступний або відповів помилкою. Текст — для логів, не для клієнта."""


class WebhookRejected(Exception):
    """Webhook НЕ прийнято: підпис не збігся або тіло зіпсоване. Тобто це, можливо, не банк."""


@dataclass(frozen=True)
class WebhookEvent:
    """Що повідомив банк, уже ПІСЛЯ перевірки підпису."""
    invoice_id: str
    status: str | None      # наш статус; None — невідомий нам (напр. reversed — повернення коштів)
    raw_status: str         # як назвав банк — для логів
    amount_kop: int | None  # сума з повідомлення — звіряємо з нашою


class PaymentProvider(Protocol):
    async def create_invoice(self, *, amount_kop: int, reference: str, description: str,
                             redirect_url: str | None, validity_seconds: int,
                             webhook_url: str | None = None) -> Invoice: ...

    async def get_status(self, invoice_id: str) -> str: ...

    async def parse_webhook(self, body: bytes, headers) -> WebhookEvent:
        """Перевіряє, що webhook справді від банку, і розбирає його.
        Не від банку / зіпсований → WebhookRejected. Банк недоступний (ключ) → PaymentError."""
        ...

    async def close(self) -> None: ...


__all__ = ["FINAL_STATUSES", "STATUSES", "Invoice", "PaymentError", "PaymentProvider",
           "WebhookEvent", "WebhookRejected"]
