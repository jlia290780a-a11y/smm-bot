import os
import io
import logging
import asyncio
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import anthropic
import openai
import urllib.request
from PIL import Image, ImageEnhance
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot, InputMediaPhoto
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ── Логирование ────────────────────────────────────────
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Конфиг ────────────────────────────────────────────
ASSISTANT_BOT_TOKEN = os.environ["ASSISTANT_BOT_TOKEN"]
CHANNEL_BOT_TOKEN   = os.environ["CHANNEL_BOT_TOKEN"]
CHANNEL_ID          = os.environ.get("CHANNEL_ID", "@neyrons_tg")
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
OPENAI_API_KEY      = os.environ["OPENAI_API_KEY"]
ALLOWED_USER_ID     = 778006973

claude  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
oai     = openai.OpenAI(api_key=OPENAI_API_KEY)
scheduler = AsyncIOScheduler()

SYSTEM_PROMPT = """Ты — СММ-ассистент Юлии, предпринимателя.

КТО ТАКАЯ ЮЛЯ:
- Построила детскую онлайн-школу скорочтения Нейронс, продала её компании Айтигенио
- Сейчас строит новый проект в сфере ИИ и автоматизации бизнеса
- Не ИИ-эксперт — предприниматель, которая использует ИИ в реальном бизнесе
- Работала с партнёром Линой больше 10 лет

СТИЛЬ ПОСТОВ:
- Пишем от первого лица, всегда через личный опыт
- Тон: честный дневник предпринимателя. Живой, разговорный, без пафоса
- Никакого корпоративного языка. Не "осуществляем внедрение", а "попробовали — вот что вышло"
- Никаких "Топ-5 нейросетей" без личного контекста
- Не писать как ИИ-эксперт — только через личную историю

СТРУКТУРА ПОСТА:
- Начало цепляет с первой строки (вопрос, неожиданный факт или личная история)
- Середина — конкретика из личного опыта
- Конец — вывод или вопрос к читателю
- Длина: 800-1500 символов для Telegram
- Эмодзи умеренно, органично

ЗАПРЕЩЕНО:
- Советы без личного опыта
- Обезличенный контент, пафос, громкие заявления
- Резкий переход сразу в тему ИИ без истории"""

# ── Состояние пользователя ────────────────────────────
drafts = {}
# draft = {
#   "text": str,
#   "topic": str,
#   "history": list,
#   "photo_bytes": bytes | None,
#   "photo_file_id": str | None,
#   "awaiting": None | "adjustment" | "schedule" | "photo_edit",
#   "edit_brightness": float,
#   "edit_contrast": float,
# }

def new_draft(topic="", text=""):
    return {
        "text": text, "topic": topic, "history": [],
        "photo_bytes": None, "photo_file_id": None,
        "awaiting": None,
        "edit_brightness": 1.0, "edit_contrast": 1.0,
    }

# ── Клавиатуры ────────────────────────────────────────
def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Опубликовать сейчас", callback_data="publish"),
         InlineKeyboardButton("⏰ Запланировать", callback_data="schedule")],
        [InlineKeyboardButton("🔄 Переписать текст", callback_data="rewrite"),
         InlineKeyboardButton("✏️ Скорректировать", callback_data="adjust")],
        [InlineKeyboardButton("🖼 Сгенерировать картинку", callback_data="gen_image"),
         InlineKeyboardButton("📷 Прикрепить своё фото", callback_data="attach_photo")],
        [InlineKeyboardButton("🎨 Редактировать фото", callback_data="edit_photo"),
         InlineKeyboardButton("🗑 Убрать фото", callback_data="remove_photo")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
    ])

def kb_edit_photo():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("☀️ Ярче", callback_data="ep_bright_up"),
         InlineKeyboardButton("🌑 Темнее", callback_data="ep_bright_down")],
        [InlineKeyboardButton("➕ Контраст", callback_data="ep_contrast_up"),
         InlineKeyboardButton("➖ Контраст", callback_data="ep_contrast_down")],
        [InlineKeyboardButton("✅ Готово", callback_data="ep_done")],
    ])

# ── Генерация текста ──────────────────────────────────
def generate_text(topic: str, history: list) -> tuple:
    messages = history + [{"role": "user", "content": f"Напиши пост для Telegram на тему: {topic}"}]
    r = claude.messages.create(model="claude-sonnet-4-6", max_tokens=1500, system=SYSTEM_PROMPT, messages=messages)
    text = r.content[0].text
    return text, messages + [{"role": "assistant", "content": text}]

def rewrite_text(history: list) -> tuple:
    messages = history + [{"role": "user", "content": "Перепиши пост по-другому — другой угол, другое начало. Сохрани стиль."}]
    r = claude.messages.create(model="claude-sonnet-4-6", max_tokens=1500, system=SYSTEM_PROMPT, messages=messages)
    text = r.content[0].text
    return text, messages + [{"role": "assistant", "content": text}]

def adjust_text(adjustment: str, history: list) -> tuple:
    messages = history + [{"role": "user", "content": f"Скорректируй пост: {adjustment}"}]
    r = claude.messages.create(model="claude-sonnet-4-6", max_tokens=1500, system=SYSTEM_PROMPT, messages=messages)
    text = r.content[0].text
    return text, messages + [{"role": "assistant", "content": text}]

# ── Генерация картинки DALL-E ─────────────────────────
def generate_image(topic: str) -> bytes:
    prompt = (
        f"Professional, warm lifestyle photo for a Telegram post about: {topic}. "
        "Style: modern, clean, inspiring. No text on image. "
        "Suitable for a personal brand of a female entrepreneur."
    )
    r = oai.images.generate(
        model="dall-e-3",
        prompt=prompt,
        size="1024x1024",
        n=1
    )
    url = r.data[0].url
    with urllib.request.urlopen(url) as resp:
        return resp.read()

# ── Редактирование фото ───────────────────────────────
def apply_edits(photo_bytes: bytes, brightness: float, contrast: float) -> bytes:
    img = Image.open(io.BytesIO(photo_bytes))
    img = ImageEnhance.Brightness(img).enhance(brightness)
    img = ImageEnhance.Contrast(img).enhance(contrast)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()

# ── Отправка превью ───────────────────────────────────
async def send_preview(update_or_query, draft: dict, edit=False):
    text = f"📝 *Текст поста:*\n\n{draft['text']}\n\n_{len(draft['text'])} символов_"
    photo = draft["photo_bytes"]

    if hasattr(update_or_query, "message"):
        send = update_or_query.message.reply_photo if photo else update_or_query.message.reply_text
    else:
        send = update_or_query.edit_message_media if (photo and edit) else None

    if photo:
        edited = apply_edits(photo, draft["edit_brightness"], draft["edit_contrast"])
        await update_or_query.message.reply_photo(
            photo=io.BytesIO(edited),
            caption=text,
            parse_mode="Markdown",
            reply_markup=kb_main()
        ) if hasattr(update_or_query, "message") else None
        if not hasattr(update_or_query, "message"):
            await update_or_query.message.reply_photo(
                photo=io.BytesIO(edited),
                caption=text,
                parse_mode="Markdown",
                reply_markup=kb_main()
            )
    else:
        if hasattr(update_or_query, "message"):
            await update_or_query.message.reply_text(text, parse_mode="Markdown", reply_markup=kb_main())
        else:
            await update_or_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb_main())

# ── Публикация ────────────────────────────────────────
async def publish_post(draft: dict):
    bot = Bot(token=CHANNEL_BOT_TOKEN)
    if draft["photo_bytes"]:
        edited = apply_edits(draft["photo_bytes"], draft["edit_brightness"], draft["edit_contrast"])
        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=io.BytesIO(edited),
            caption=draft["text"],
            parse_mode="Markdown"
        )
    else:
        await bot.send_message(chat_id=CHANNEL_ID, text=draft["text"], parse_mode="Markdown")

# ── Хэндлеры ─────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    await update.message.reply_text(
        "Привет, Юля! 👋\n\n"
        "Я твой СММ-ассистент. Напиши тему поста — и я напишу его в твоём стиле.\n\n"
        "Что умею:\n"
        "• Писать посты через Claude\n"
        "• Генерировать картинки через DALL-E\n"
        "• Принимать и редактировать твои фото\n"
        "• Планировать публикации по времени\n\n"
        "Просто напиши тему — начнём!",
        parse_mode="Markdown"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    user_id = update.effective_user.id
    draft = drafts.get(user_id, new_draft())

    # Ждём фото от пользователя
    if draft.get("awaiting") == "attach_photo":
        if update.message.photo:
            thinking = await update.message.reply_text("📥 Загружаю фото...")
            file = await update.message.photo[-1].get_file()
            buf = io.BytesIO()
            await file.download_to_memory(buf)
            draft["photo_bytes"] = buf.getvalue()
            draft["photo_file_id"] = update.message.photo[-1].file_id
            draft["awaiting"] = None
            drafts[user_id] = draft
            await thinking.delete()
            await update.message.reply_text("✅ Фото добавлено! Вот превью поста:")
            await send_preview(update, draft)
        else:
            await update.message.reply_text("Пожалуйста, отправь фото (не файл).")
        return

    # Ждём корректировку текста
    if draft.get("awaiting") == "adjustment":
        draft["awaiting"] = None
        thinking = await update.message.reply_text("✏️ Корректирую...")
        try:
            new_text, new_history = adjust_text(update.message.text, draft["history"])
            draft["text"] = new_text
            draft["history"] = new_history
            drafts[user_id] = draft
            await thinking.delete()
            await send_preview(update, draft)
        except Exception as e:
            await thinking.edit_text(f"❌ Ошибка: {e}")
        return

    # Ждём дату/время для планировщика
    if draft.get("awaiting") == "schedule":
        draft["awaiting"] = None
        try:
            dt = datetime.strptime(update.message.text.strip(), "%d.%m.%Y %H:%M")
            draft_copy = dict(draft)
            drafts[user_id] = draft
            scheduler.add_job(
                publish_post, "date", run_date=dt,
                args=[draft_copy],
                id=f"post_{user_id}_{dt.timestamp()}"
            )
            await update.message.reply_text(
                f"⏰ Пост запланирован на {dt.strftime('%d.%m.%Y в %H:%M')}!\n"
                "Я опубликую его автоматически."
            )
            del drafts[user_id]
        except ValueError:
            await update.message.reply_text("❌ Неверный формат. Напишите: `31.12.2025 15:00`", parse_mode="Markdown")
        return

    # Новая тема поста
    topic = update.message.text
    thinking = await update.message.reply_text("✍️ Пишу пост...")
    try:
        post_text, history = generate_text(topic, [])
        drafts[user_id] = {**new_draft(topic, post_text), "history": history}
        await thinking.delete()
        await send_preview(update, drafts[user_id])
    except Exception as e:
        await thinking.edit_text(f"❌ Ошибка: {e}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Принимаем фото в любой момент."""
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    user_id = update.effective_user.id
    draft = drafts.get(user_id)
    if not draft:
        await update.message.reply_text("Сначала напиши тему поста, потом отправь фото.")
        return
    thinking = await update.message.reply_text("📥 Загружаю фото...")
    file = await update.message.photo[-1].get_file()
    buf = io.BytesIO()
    await file.download_to_memory(buf)
    draft["photo_bytes"] = buf.getvalue()
    draft["photo_file_id"] = update.message.photo[-1].file_id
    draft["awaiting"] = None
    drafts[user_id] = draft
    await thinking.delete()
    await update.message.reply_text("✅ Фото добавлено!")
    await send_preview(update, draft)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    if user_id != ALLOWED_USER_ID:
        return
    await query.answer()
    action = query.data
    draft = drafts.get(user_id)

    if action == "publish":
        if not draft:
            await query.edit_message_text("❌ Черновик не найден. Напиши тему заново.")
            return
        try:
            await publish_post(draft)
            await query.edit_message_text(f"✅ Опубликовано в {CHANNEL_ID}!")
            del drafts[user_id]
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка публикации: {e}")

    elif action == "schedule":
        if not draft:
            await query.edit_message_text("❌ Черновик не найден.")
            return
        drafts[user_id]["awaiting"] = "schedule"
        await query.edit_message_text(
            "⏰ Напиши дату и время публикации в формате:\n`31.12.2025 15:00`",
            parse_mode="Markdown"
        )

    elif action == "rewrite":
        if not draft:
            await query.edit_message_text("❌ Черновик не найден.")
            return
        await query.edit_message_text("🔄 Переписываю...")
        try:
            new_text, new_history = rewrite_text(draft["history"])
            drafts[user_id]["text"] = new_text
            drafts[user_id]["history"] = new_history
            await query.message.reply_text(
                f"📝 *Новый вариант:*\n\n{new_text}\n\n_{len(new_text)} символов_",
                parse_mode="Markdown", reply_markup=kb_main()
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка: {e}")

    elif action == "adjust":
        if not draft:
            await query.edit_message_text("❌ Черновик не найден.")
            return
        drafts[user_id]["awaiting"] = "adjustment"
        await query.edit_message_text(
            "✏️ *Напиши что изменить:*\n\nНапример: «сделай короче», «другое начало», «убери эмодзи»",
            parse_mode="Markdown"
        )

    elif action == "gen_image":
        if not draft:
            await query.edit_message_text("❌ Черновик не найден.")
            return
        await query.edit_message_text("🎨 Генерирую картинку через DALL-E...")
        try:
            photo_bytes = generate_image(draft["topic"])
            drafts[user_id]["photo_bytes"] = photo_bytes
            await query.message.reply_photo(
                photo=io.BytesIO(photo_bytes),
                caption=f"🖼 Картинка готова!\n\n_{draft['topic']}_",
                parse_mode="Markdown", reply_markup=kb_main()
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка генерации: {e}")

    elif action == "attach_photo":
        if not draft:
            await query.edit_message_text("❌ Черновик не найден.")
            return
        drafts[user_id]["awaiting"] = "attach_photo"
        await query.edit_message_text("📷 Отправь фото следующим сообщением.")

    elif action == "edit_photo":
        if not draft or not draft.get("photo_bytes"):
            await query.edit_message_text("❌ Сначала добавь фото.")
            return
        edited = apply_edits(draft["photo_bytes"], draft["edit_brightness"], draft["edit_contrast"])
        await query.message.reply_photo(
            photo=io.BytesIO(edited),
            caption=f"🎨 Редактирование фото\nЯркость: {draft['edit_brightness']:.1f} | Контраст: {draft['edit_contrast']:.1f}",
            reply_markup=kb_edit_photo()
        )

    elif action == "remove_photo":
        if draft:
            drafts[user_id]["photo_bytes"] = None
            drafts[user_id]["photo_file_id"] = None
        await query.edit_message_text("🗑 Фото удалено. Пост будет без фото.", reply_markup=kb_main())

    elif action == "cancel":
        if user_id in drafts:
            del drafts[user_id]
        await query.edit_message_text("❌ Отменено. Напиши новую тему когда будешь готова.")

    # Редактирование фото
    elif action.startswith("ep_"):
        if not draft:
            return
        if action == "ep_bright_up":
            drafts[user_id]["edit_brightness"] = min(2.0, draft["edit_brightness"] + 0.1)
        elif action == "ep_bright_down":
            drafts[user_id]["edit_brightness"] = max(0.5, draft["edit_brightness"] - 0.1)
        elif action == "ep_contrast_up":
            drafts[user_id]["edit_contrast"] = min(2.0, draft["edit_contrast"] + 0.1)
        elif action == "ep_contrast_down":
            drafts[user_id]["edit_contrast"] = max(0.5, draft["edit_contrast"] - 0.1)
        elif action == "ep_done":
            await query.edit_message_caption(
                caption="✅ Изменения сохранены!",
                reply_markup=kb_main()
            )
            return

        d = drafts[user_id]
        edited = apply_edits(d["photo_bytes"], d["edit_brightness"], d["edit_contrast"])
        await query.edit_message_media(
            media=InputMediaPhoto(
                media=io.BytesIO(edited),
                caption=f"🎨 Яркость: {d['edit_brightness']:.1f} | Контраст: {d['edit_contrast']:.1f}"
            ),
            reply_markup=kb_edit_photo()
        )

# ── Запуск ────────────────────────────────────────────
def main():
    app = Application.builder().token(ASSISTANT_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    scheduler.start()
    logger.info("СММ-бот запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
