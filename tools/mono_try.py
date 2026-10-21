"""
Спробувати mono БЕЗ бота: створити рахунок і стежити за статусом.

Запуск:  python -m tools.mono_try          (рахунок на 10 грн)
         python -m tools.mono_try 490      (на 490 грн)

Навіщо: якщо в боті щось не так з оплатою, цей скрипт одразу покаже, де проблема —
у mono (токен, мережа, відповідь банку) чи в нашому коді. Діагностика по шарах:
спершу перевіряємо найнижчий шар окремо.
Потрібен лише MONO_TOKEN у .env.
"""
import asyncio
import os
import sys

from dotenv import load_dotenv

from bot.payments import PaymentError
from bot.payments.mono import MonoProvider

load_dotenv()


async def main() -> None:
    token = os.getenv("MONO_TOKEN", "").strip()
    if not token:
        sys.exit("❌ Не задано MONO_TOKEN у .env")
    uah = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    provider = MonoProvider(token)
    try:
        invoice = await provider.create_invoice(
            amount_kop=uah * 100, reference="mono-try", description=f"Тест mono_try — {uah} грн",
            redirect_url=None, validity_seconds=600)
        print(f"✅ Рахунок створено: {invoice.invoice_id}")
        print(f"   Відкрий і оплати тестовою карткою 4242 4242 4242 4242:\n   {invoice.page_url}\n")
        last = None
        for _ in range(60):  # до 5 хвилин, кожні 5 секунд
            status = await provider.get_status(invoice.invoice_id)
            if status != last:
                print(f"   статус: {status}")
                last = status
            if status in ("success", "failure", "expired"):
                break
            await asyncio.sleep(5)
    except PaymentError as e:
        sys.exit(f"❌ {e}")
    finally:
        await provider.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
