"""Фальшивий банк для тестів: той самий інтерфейс, що MonoProvider, але все в пам'яті.

Тест сам вирішує, що «відповість банк»: успіх, відмова, прострочення, «банк лежить».
"""
from itertools import count

from bot.payments import Invoice, PaymentError


class FakeProvider:
    def __init__(self) -> None:
        self.statuses: dict[str, str] = {}   # invoice_id → статус
        self.created: list[dict] = []        # з якими параметрами створювали рахунки
        self.status_calls = 0
        self.down = False                    # True → «банк недоступний»
        self._ids = count(1)

    async def create_invoice(self, **kwargs) -> Invoice:
        if self.down:
            raise PaymentError("fake: банк недоступний")
        invoice_id = f"inv{next(self._ids)}"
        self.created.append(kwargs | {"invoice_id": invoice_id})
        self.statuses[invoice_id] = "created"
        return Invoice(invoice_id=invoice_id, page_url=f"https://pay.test/{invoice_id}")

    async def get_status(self, invoice_id: str) -> str:
        self.status_calls += 1
        if self.down:
            raise PaymentError("fake: банк недоступний")
        return self.statuses[invoice_id]

    def pay(self, invoice_id: str = "inv1", status: str = "success") -> None:
        """«Людина оплатила» (або картку відхилено / рахунок згорів)."""
        self.statuses[invoice_id] = status

    async def close(self) -> None:
        pass
