#!/usr/bin/env python3
"""
Telegram Bot for managing the ICP watchlist.
Run locally once to configure, then push WATCHLIST_JSON to GitHub Secrets.

Commands:
  /start   - Welcome message
  /list    - Show current watchlist
  /add     - Interactive wizard to add a new target
  /remove  - Remove a target
  /check   - Run a one-shot check right now (requires WATCHLIST_JSON + browser)
  /export  - Print WATCHLIST_JSON to copy into GitHub Secrets
"""

import os
import json
import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ConversationHandler, MessageHandler, filters, ContextTypes
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

TOKEN = os.environ["TELEGRAM_TOKEN"]
WATCHLIST_FILE = "data/watchlist.json"

# Conversation states
(ASK_PROVINCE_CODE, ASK_PROVINCE_NAME,
 ASK_OFFICE_CODE, ASK_OFFICE_NAME,
 ASK_TRAMITE, ASK_EXPECTED_LOT) = range(6)

# ── Helpers ───────────────────────────────────────────────────────────────────
def load_watchlist() -> list:
    if os.path.exists(WATCHLIST_FILE):
        return json.loads(open(WATCHLIST_FILE).read())
    return []

def save_watchlist(wl: list):
    os.makedirs("data", exist_ok=True)
    open(WATCHLIST_FILE, "w").write(json.dumps(wl, ensure_ascii=False, indent=2))

# ── Commands ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👮 <b>ICP Monitor Bot</b>\n\n"
        "Слежу за номерами лотов на сайте записи в полицию Испании.\n\n"
        "Команды:\n"
        "/list — список отслеживаемых участков\n"
        "/add — добавить участок\n"
        "/remove — убрать участок\n"
        "/export — экспорт JSON для GitHub Secrets\n",
        parse_mode="HTML"
    )

async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    wl = load_watchlist()
    if not wl:
        await update.message.reply_text("Список пуст. Добавь участок через /add")
        return
    lines = []
    for i, t in enumerate(wl, 1):
        lot_info = f" (ожидаем лот: <b>{t['expected_lot']}</b>)" if t.get("expected_lot") else ""
        lines.append(f"{i}. {t['province_name']} → {t['office_name']}\n"
                     f"   📋 {t['tramite']}{lot_info}")
    await update.message.reply_text(
        "📋 <b>Отслеживаемые участки:</b>\n\n" + "\n\n".join(lines),
        parse_mode="HTML"
    )

async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    wl = load_watchlist()
    json_str = json.dumps(wl, ensure_ascii=False)
    await update.message.reply_text(
        "Скопируй в GitHub Secrets как <b>WATCHLIST_JSON</b>:\n\n"
        f"<code>{json_str}</code>",
        parse_mode="HTML"
    )

# ── Add wizard ────────────────────────────────────────────────────────────────
async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Добавляем новый участок.\n\n"
        "Шаг 1/6: Введи <b>код провинции</b> (число).\n\n"
        "Примеры: <code>33</code> = Asturias, <code>28</code> = Madrid, "
        "<code>08</code> = Barcelona, <code>46</code> = Valencia\n\n"
        "Код можно найти в URL при выборе провинции на сайте или в README.",
        parse_mode="HTML"
    )
    return ASK_PROVINCE_CODE

async def got_province_code(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["province"] = update.message.text.strip()
    await update.message.reply_text(
        "Шаг 2/6: Введи <b>название провинции</b> (для удобного отображения).\n"
        "Например: <code>Asturias</code>",
        parse_mode="HTML"
    )
    return ASK_PROVINCE_NAME

async def got_province_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["province_name"] = update.message.text.strip()
    await update.message.reply_text(
        "Шаг 3/6: Введи <b>код комиссарии</b> (число из списка на сайте).\n\n"
        "Зайди на сайт, выбери провинцию, посмотри в исходнике value у нужной комиссарии.\n"
        "Или введи <code>0</code> чтобы я взял первую доступную.",
        parse_mode="HTML"
    )
    return ASK_OFFICE_CODE

async def got_office_code(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["office"] = update.message.text.strip()
    await update.message.reply_text(
        "Шаг 4/6: Введи <b>название комиссарии</b>.\n"
        "Например: <code>Oviedo</code>",
        parse_mode="HTML"
    )
    return ASK_OFFICE_NAME

async def got_office_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["office_name"] = update.message.text.strip()
    await update.message.reply_text(
        "Шаг 5/6: Введи <b>ключевое слово трамита</b> (часть названия).\n\n"
        "Примеры:\n"
        "• <code>RECOGIDA</code> — для POLICIA-RECOGIDA DE TARJETA\n"
        "• <code>HUELLAS</code> — для TOMA DE HUELLAS (TIE)\n"
        "• <code>NIE</code> — для NIE\n\n"
        "Скрипт найдёт первый трамит, содержащий это слово.",
        parse_mode="HTML"
    )
    return ASK_TRAMITE

async def got_tramite(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["tramite"] = update.message.text.strip().upper()
    await update.message.reply_text(
        "Шаг 6/6: Введи <b>ожидаемый номер лота</b> (или /skip чтобы пропустить).\n\n"
        "Если лот ≥ этого числа — уведомление будет выделено особо. 🎯",
        parse_mode="HTML"
    )
    return ASK_EXPECTED_LOT

async def got_expected_lot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    ctx.user_data["expected_lot"] = "" if text == "/skip" else text
    return await _finish_add(update, ctx)

async def _finish_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    target = {
        "province": ctx.user_data["province"],
        "province_name": ctx.user_data["province_name"],
        "office": ctx.user_data["office"],
        "office_name": ctx.user_data["office_name"],
        "tramite": ctx.user_data["tramite"],
        "expected_lot": ctx.user_data.get("expected_lot", ""),
    }
    wl = load_watchlist()
    wl.append(target)
    save_watchlist(wl)

    await update.message.reply_text(
        f"✅ Добавлено!\n\n"
        f"📍 {target['province_name']} → {target['office_name']}\n"
        f"📋 {target['tramite']}\n"
        f"🎯 Ожидаемый лот: {target['expected_lot'] or 'не задан'}\n\n"
        f"Теперь экспортируй через /export и обнови GitHub Secret <b>WATCHLIST_JSON</b>.",
        parse_mode="HTML"
    )
    ctx.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END

# ── Remove ────────────────────────────────────────────────────────────────────
async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    wl = load_watchlist()
    if not wl:
        await update.message.reply_text("Список пуст.")
        return
    buttons = []
    for i, t in enumerate(wl):
        label = f"{t['province_name']} → {t['office_name']} ({t['tramite'][:20]})"
        buttons.append([InlineKeyboardButton(label, callback_data=f"remove_{i}")])
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="remove_cancel")])
    await update.message.reply_text(
        "Выбери участок для удаления:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def handle_remove_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "remove_cancel":
        await query.edit_message_text("Отменено.")
        return
    idx = int(data.split("_")[1])
    wl = load_watchlist()
    if 0 <= idx < len(wl):
        removed = wl.pop(idx)
        save_watchlist(wl)
        await query.edit_message_text(
            f"🗑 Удалено: {removed['province_name']} → {removed['office_name']}"
        )
    else:
        await query.edit_message_text("Ошибка: элемент не найден.")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TOKEN).build()

    add_conv = ConversationHandler(
        entry_points=[CommandHandler("add", cmd_add)],
        states={
            ASK_PROVINCE_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_province_code)],
            ASK_PROVINCE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_province_name)],
            ASK_OFFICE_CODE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, got_office_code)],
            ASK_OFFICE_NAME:   [MessageHandler(filters.TEXT & ~filters.COMMAND, got_office_name)],
            ASK_TRAMITE:       [MessageHandler(filters.TEXT & ~filters.COMMAND, got_tramite)],
            ASK_EXPECTED_LOT:  [MessageHandler(filters.TEXT, got_expected_lot)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CallbackQueryHandler(handle_remove_callback, pattern="^remove_"))
    app.add_handler(add_conv)

    log.info("Bot started. Press Ctrl+C to stop.")
    app.run_polling()

if __name__ == "__main__":
    main()
