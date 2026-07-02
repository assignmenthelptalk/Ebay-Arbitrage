"""
Telegram approval service for the fulfillment gate.

Runs as its own long-lived process next to the API. It polls the gate for
orders in `pending_review`, pushes each one to your Telegram chat with
Approve / Reject buttons, and turns your tap into a call to the gate's
approve()/reject(). It talks to the gate directly (shared queue file, file
locked), so it does NOT need the API key.

SECURITY: only Telegram user IDs listed in TELEGRAM_ALLOWED_IDS can approve
or reject anything. Everyone else is ignored. Set this or the bot refuses to
start — an approval bot anyone can press buttons on is worse than none.

Config (env):
    TELEGRAM_BOT_TOKEN     from @BotFather
    TELEGRAM_ALLOWED_IDS   comma-separated numeric user IDs allowed to act
    TELEGRAM_CHAT_ID       chat to send notifications to (usually your own ID)
    APPROVAL_BUFFER_PCT    one-tap ceiling = amazon_price * (1 + this)  (default 0.10)
    POLL_INTERVAL          seconds between pending checks               (default 30)

Install:  pip install "python-telegram-bot>=20,<22"
Run:      python telegram_approver.py
"""

import os
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
)

import fulfillment_gate as gate

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tg-approver")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
ALLOWED_IDS = {
    int(x) for x in os.getenv("TELEGRAM_ALLOWED_IDS", "").split(",") if x.strip()
}
BUFFER = float(os.getenv("APPROVAL_BUFFER_PCT", "0.10"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))

if not ALLOWED_IDS:
    raise SystemExit("Refusing to start: TELEGRAM_ALLOWED_IDS is empty.")

# order_ids we've already sent a card for, so we don't spam every poll
_notified: set[str] = set()


def _allowed(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in ALLOWED_IDS)


def _suggested_ceiling(amazon_price: float) -> float:
    return round(float(amazon_price) * (1 + BUFFER), 2)


def _card_text(job: dict) -> str:
    price = float(job["amazon_price"])
    margin = job.get("meta", {}).get("margin")
    lines = [
        f"🔔 *Order {job['order_id']}* needs approval",
        f"Amazon price on file: *${price:.2f}*",
    ]
    if margin is not None:
        lines.append(f"Margin: {margin}")
    lines.append(f"One-tap approves up to *${_suggested_ceiling(price):.2f}* "
                 f"(price +{int(BUFFER*100)}%).")
    lines.append("For a different ceiling: `/approve " + str(job["order_id"]) + " <amount>`")
    return "\n".join(lines)


def _card_buttons(order_id: str, ceiling: float) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ Approve ≤ ${ceiling:.2f}", callback_data=f"ap:{order_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"rj:{order_id}"),
    ]])


# --- notifier: poll the gate and push new pending orders ------------------

async def poll_pending(context: ContextTypes.DEFAULT_TYPE):
    try:
        pending = gate.list_pending()
    except Exception as e:  # never let a bad poll kill the loop
        log.exception("poll failed: %s", e)
        return

    current_ids = {j["order_id"] for j in pending}
    _notified.intersection_update(current_ids)  # forget orders no longer pending

    for job in pending:
        oid = job["order_id"]
        if oid in _notified:
            continue
        ceiling = _suggested_ceiling(job["amazon_price"])
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=_card_text(job),
            parse_mode="Markdown",
            reply_markup=_card_buttons(oid, ceiling),
        )
        _notified.add(oid)


# --- button taps ----------------------------------------------------------

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not _allowed(update):
        await query.answer("Not authorized.", show_alert=True)
        return

    action, oid = query.data.split(":", 1)
    approver = f"tg:{update.effective_user.id}"

    try:
        if action == "ap":
            # re-read the order so we approve against current data, not the card
            job = next((j for j in gate.list_pending() if j["order_id"] == oid), None)
            if not job:
                await query.edit_message_text(f"Order {oid} is no longer pending.")
                return
            ceiling = _suggested_ceiling(job["amazon_price"])
            gate.approve(oid, ceiling, approver)
            await query.edit_message_text(
                f"✅ Order {oid} approved up to ${ceiling:.2f} by you.\n"
                f"The bot will buy only if the live total is at or under that."
            )
        elif action == "rj":
            gate.reject(oid, reason="rejected via Telegram", approver=approver)
            await query.edit_message_text(f"❌ Order {oid} rejected. The bot will not buy it.")
    except gate.GateError as e:
        await query.edit_message_text(f"⚠️ Order {oid}: {e.detail}")


# --- commands -------------------------------------------------------------

async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return
    pending = gate.list_pending()
    if not pending:
        await update.message.reply_text("Nothing waiting for approval.")
        return
    for job in pending:
        ceiling = _suggested_ceiling(job["amazon_price"])
        await update.message.reply_text(
            _card_text(job), parse_mode="Markdown",
            reply_markup=_card_buttons(job["order_id"], ceiling),
        )


async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return
    try:
        oid, amount = context.args[0], float(context.args[1])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /approve <order_id> <amount>")
        return
    try:
        gate.approve(oid, amount, approver=f"tg:{update.effective_user.id}")
        await update.message.reply_text(f"✅ Order {oid} approved up to ${amount:.2f}.")
    except gate.GateError as e:
        await update.message.reply_text(f"⚠️ {e.detail}")


async def cmd_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return
    try:
        oid = context.args[0]
        reason = " ".join(context.args[1:]) or "rejected via Telegram"
    except IndexError:
        await update.message.reply_text("Usage: /reject <order_id> <reason>")
        return
    try:
        gate.reject(oid, reason=reason, approver=f"tg:{update.effective_user.id}")
        await update.message.reply_text(f"✅ Order {oid} rejected.")
    except gate.GateError as e:
        await update.message.reply_text(f"⚠️ {e.detail}")


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.job_queue.run_repeating(poll_pending, interval=POLL_INTERVAL, first=5)
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("reject", cmd_reject))
    app.add_handler(CallbackQueryHandler(on_button))
    log.info("Telegram approver running. Authorized IDs: %s", ALLOWED_IDS)
    app.run_polling()


if __name__ == "__main__":
    main()
