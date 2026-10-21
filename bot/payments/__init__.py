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


class PaymentProvider(Protocol):
    async def create_invoice(self, *, amount_kop: int, reference: str, description: str,
                             redirect_url: str | None, validity_seconds: int) -> Invoice: ...

    async def get_status(self, invoice_id: str) -> str: ...

    async def close(self) -> None: ...


__all__ = ["FINAL_STATUSES", "STATUSES", "Invoice", "PaymentError", "PaymentProvider"]
