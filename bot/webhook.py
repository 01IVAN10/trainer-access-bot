"""
HTTP-сервер бота: сюди mono надсилає webhook «статус рахунку змінився».

Досі бот лише сам ходив до інших (Telegram, mono). Тепер до нього стукають ззовні —
а отже, будь-хто з інтернету. Тому правило «все від користувача — неперевірене»
тут у найчистішому вигляді:
  1) перевіряємо підпис X-Sign (bot/payments/mono.py) — ДО будь-якої іншої дії;
  2) шукаємо платіж за invoiceId у НАШІЙ базі (чужий/вигаданий — ігноруємо);
  3) звіряємо суму з нашою (банк підписав — але перевірка коштує один рядок);
  4) лише тоді — record_status (та сама функція, що для кнопки «Перевірити»).

Що відповідаємо mono — важливо: на НЕ-200 він повторить (до 3 разів).
  200 — прийняли (або це не наше — повторювати немає сенсу)
  403 — підпис не збігся (це не mono; повтор не допоможе, але й 200 не заслуговує)
  503 — наша тимчасова проблема (не дістали ключ, впала база) → нехай mono спробує ще
Окремий маршрут GET / — «я живий» для Railway і для перевірки тунелю.
"""
import logging

from aiohttp import web
from aiogram import Bot

from bot import billing, db
from bot.access import ChannelAccess
from bot.config import Config
from bot.payments import PaymentError, PaymentProvider, WebhookRejected
from bot.plans import Plan
from bot.subscription import utcnow

log = logging.getLogger(__name__)

WEBHOOK_PATH = "/webhooks/mono"
# Ключі для зберігання залежностей у web.Application (так aiohttp радить замість рядків).
BOT, PROVIDER, PLANS, CONFIG, ACCESS = (web.AppKey(n) for n in ("bot", "provider", "plans", "config", "access"))


async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def mono_webhook(request: web.Request) -> web.Response:
    body = await request.read()  # СИРІ байти — саме їх підписав mono
    provider: PaymentProvider = request.app[PROVIDER]
    try:
        event = await provider.parse_webhook(body, request.headers)
    except WebhookRejected as e:
        log.warning("WEBHOOK відхилено (%s) від %s", e, request.remote)
        return web.Response(status=403, text="bad signature")
    except PaymentError as e:
        log.error("WEBHOOK: не вдалося перевірити підпис: %s", e)
        return web.Response(status=503, text="try later")

    payment = await db.get_payment_by_invoice(event.invoice_id)
    if payment is None:
        # Напр. рахунок з tools/mono_try або з іншого оточення. Не наш — і не помилка.
        log.info("WEBHOOK: невідомий рахунок %s (%s) — ігноруємо", event.invoice_id, event.raw_status)
        return web.Response(text="unknown invoice")
    if event.status is None:
        log.warning("WEBHOOK: платіж #%s — статус %r, який ми не обробляємо", payment["id"], event.raw_status)
        return web.Response(text="ignored")
    if event.amount_kop is not None and event.amount_kop != payment["amount_kop"]:
        # Підпис справжній, але сума не наша — щось дуже не так. Нічого не зараховуємо, кричимо в лог.
        log.error("WEBHOOK: платіж #%s — сума %s ≠ нашій %s. НЕ зараховано!",
                  payment["id"], event.amount_kop, payment["amount_kop"])
        return web.Response(text="amount mismatch")

    try:
        sub = await billing.record_status(payment["id"], event.status, request.app[PLANS], utcnow(),
                                         request.app[BOT], request.app[CONFIG])
    except Exception:
        log.exception("WEBHOOK: помилка обробки платежу #%s", payment["id"])
        return web.Response(status=503, text="try later")  # mono повторить
    log.info("WEBHOOK: платіж #%s → %s", payment["id"], event.status)
    if event.status != "success" or sub is not None:  # прибрати кнопки зі старого рахунку (крок 8)
        await billing.close_invoice(request.app[BOT], payment, event.status)
    if sub is not None:  # саме цей webhook продовжив підписку → кажемо людині
        await billing.notify_paid(request.app[BOT], sub, request.app[PLANS], request.app[CONFIG].timezone,
                                  request.app[ACCESS])
    return web.Response(text="ok")


def build_web_app(bot: Bot, provider: PaymentProvider, plans: dict[str, Plan], config: Config,
                  access: ChannelAccess) -> web.Application:
    app = web.Application(client_max_size=64 * 1024)  # webhook mono — кілька КБ; більше не приймаємо
    app[BOT], app[PROVIDER], app[PLANS], app[CONFIG], app[ACCESS] = bot, provider, plans, config, access
    app.router.add_get("/", health)
    app.router.add_post(WEBHOOK_PATH, mono_webhook)
    return app
