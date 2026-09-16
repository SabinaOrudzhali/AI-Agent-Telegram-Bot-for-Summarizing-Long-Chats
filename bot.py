import os
import asyncio
import logging
from collections import defaultdict
from datetime import datetime

from dotenv import load_dotenv
from gigachat import GigaChat
from gigachat.models import Chat, Messages, MessagesRole
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

load_dotenv()

TG_TOKEN = os.getenv("TG_TOKEN")
GIGACHAT_AUTH_KEY = os.getenv("GIGACHAT_AUTH_KEY")
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("summary_bot")

SYSTEM_PROMPT = """Ты делаешь краткое саммари переписки.

Формат:

📌 ТЕМЫ
✅ РЕШЕНИЯ
⚠️ ВОПРОСЫ И ЗАДАЧИ
📅 ДЕДЛАЙНЫ

Кратко, по делу, без воды.
"""

CHUNK_SIZE = 12000
OVERLAP = 500
BUFFER_WAIT_SEC = 2.0

buffer = defaultdict(list)
stats = defaultdict(int)


def split_text(text):
    if len(text) <= CHUNK_SIZE:
        return [text]

    chunks = []
    start = 0

    while start < len(text):
        end = min(start + CHUNK_SIZE, len(text))

        if end < len(text):
            nl = text.rfind("\\n\\n", start, end)
            if nl > start + CHUNK_SIZE // 2:
                end = nl

        chunks.append(text[start:end])

        if end >= len(text):
            break

        start = end - OVERLAP

    return chunks


def summarize_chunk(text):
    with GigaChat(
        credentials=GIGACHAT_AUTH_KEY,
        scope=GIGACHAT_SCOPE,
        verify_ssl_certs=False,
    ) as giga:

        payload = Chat(
            messages=[
                Messages(role=MessagesRole.SYSTEM, content=SYSTEM_PROMPT),
                Messages(role=MessagesRole.USER, content=text),
            ],
            temperature=0.3,
            max_tokens=800,
        )

        response = giga.chat(payload)
        return response.choices[0].message.content


def merge_summaries(parts):
    if len(parts) == 1:
        return parts[0]

    joined = "\\n\\n---\\n\\n".join(parts)

    prompt = (
        "Объедини несколько саммари в одно. "
        "Удали дубли. Сохрани формат: "
        "ТЕМЫ / РЕШЕНИЯ / ВОПРОСЫ / ДЕДЛАЙНЫ."
    )

    with GigaChat(
        credentials=GIGACHAT_AUTH_KEY,
        scope=GIGACHAT_SCOPE,
        verify_ssl_certs=False,
    ) as giga:

        payload = Chat(
            messages=[
                Messages(role=MessagesRole.SYSTEM, content=prompt),
                Messages(role=MessagesRole.USER, content=joined),
            ],
            temperature=0.2,
            max_tokens=1000,
        )

        response = giga.chat(payload)
        return response.choices[0].message.content


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Перешли одно или несколько сообщений.\n"
        "Я сделаю краткое саммари через GigaChat.\n\n"
        "/summary — собрать саммари\n"
        "/stats — статистика"
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(
        f"Саммари создано: {stats[uid]}"
    )


async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if not buffer[uid]:
        await update.message.reply_text("Буфер пуст.")
        return

    await process_buffer(update, uid)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    text = update.message.text or update.message.caption or ""

    if not text.strip():
        return

    buffer[uid].append((datetime.utcnow(), text))

    await asyncio.sleep(BUFFER_WAIT_SEC)

    last_time = buffer[uid][-1][0]

    if (datetime.utcnow() - last_time).total_seconds() < BUFFER_WAIT_SEC - 0.3:
        return

    await process_buffer(update, uid)


async def process_buffer(update: Update, uid: int):
    full_text = "\\n\\n".join(t for _, t in buffer[uid])
    buffer[uid] = []

    if len(full_text) < 1:
        await update.message.reply_text(
            "Слишком короткий текст."
        )
        return

    chunks = split_text(full_text)

    msg = await update.message.reply_text(
        f"⏳ Обрабатываю... частей: {len(chunks)}"
    )

    try:
        loop = asyncio.get_running_loop()

        parts = []

        for chunk in chunks:
            part = await loop.run_in_executor(
                None,
                summarize_chunk,
                chunk
            )
            parts.append(part)

        final_summary = await loop.run_in_executor(
            None,
            merge_summaries,
            parts
        )

        stats[uid] += 1

        await msg.edit_text(final_summary)

    except Exception as e:
        await msg.edit_text(f"Ошибка: {e}")


def main():
    if not TG_TOKEN:
        raise RuntimeError("Не найден TG_TOKEN в .env")

    app = Application.builder().token(TG_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(CommandHandler("stats", cmd_stats))

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            on_text
        )
    )

    log.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
