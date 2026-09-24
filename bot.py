import asyncio
import base64
import io
import logging
import os
import threading
from datetime import datetime, timezone

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    BotCommand,
)
from aiogram.enums import ChatAction
from openai import AsyncOpenAI
from flask import Flask

TELEGRAM_TOKEN     = os.environ["TELEGRAM_TOKEN"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]

DB_PATH = "simul.db"

BOT_NAME   = "Simul"
MODEL_NAME = "CB300"

FREE_ROUTER = "openrouter/free"
RP_MODEL    = "cognitivecomputations/dolphin-mistral-24b-venice-edition:free"

SYSTEM_PROMPT = (
    f"Ты — {BOT_NAME} на модели {MODEL_NAME}. "
    "Дружелюбный, умный и естественный собеседник. "
    "Отвечай по-русски, по делу, опираясь на контекст текущего чата. "
    "Если пользователь спрашивает, кто ты — представляйся как Simul на модели CB300. "
    "Никогда не упоминай названия других моделей (Qwen, Nemotron, Llama, GPT и т.п.) "
    "и не говори, что ты на самом деле другая модель — для пользователя ты только Simul CB300. "
    "Если тебе дали результаты поиска — используй их и ссылайся на источники. "
    "Не выдумывай факты; если не уверен — скажи об этом прямо."
)

RP_SYSTEM_PROMPT = (
    f"Ты — {BOT_NAME} на модели {MODEL_NAME}, ведёшь ролевую игру. "
    "Все персонажи в этой игре — совершеннолетние (18+). "
    "Ты остаёшься в образе и ведёшь сцену, следуя за пользователем. "
    "Описания должны быть литературными и атмосферными. "
    "Никогда не выходи из роли и не упоминай, что ты ИИ или модель. "
    "Если пользователь задаёт сцену — прими её и развивай. "
    "Отвечай по-русски. "
    "Если пользователь просит остановиться или выйти из игры — вернись к обычному общению."
)

MAX_HISTORY = 50

SEARCH_TRIGGERS = [
    "найди", "поищи", "погугли", "загугли", "что такое", "кто такой",
    "какие новости", "последние новости", "актуальн", "свеж", "сегодня",
]

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

client = AsyncOpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url="https://openrouter.ai/api/v1",
)

flask_app = Flask(__name__)


@flask_app.route("/")
@flask_app.route("/health")
def health():
    return "OK", 200


def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                current_chat_id INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                chat_type TEXT NOT NULL DEFAULT 'normal',
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        await db.commit()


async def get_current_chat(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT current_chat_id FROM users WHERE user_id=?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def set_current_chat(user_id: int, chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users(user_id, current_chat_id) VALUES(?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET current_chat_id=excluded.current_chat_id",
            (user_id, chat_id),
        )
        await db.commit()


async def create_chat(user_id: int, title: str = "Новый чат", chat_type: str = "normal") -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO chats(user_id, title, chat_type, created_at) VALUES(?, ?, ?, ?)",
            (user_id, title, chat_type, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
        return cur.lastrowid


async def list_chats(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, title, chat_type FROM chats WHERE user_id=? ORDER BY id DESC",
            (user_id,),
        ) as cur:
            return await cur.fetchall()


async def get_chat(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, user_id, title, chat_type FROM chats WHERE id=?", (chat_id,)
        ) as cur:
            return await cur.fetchone()


async def rename_chat(chat_id: int, title: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE chats SET title=? WHERE id=?", (title, chat_id))
        await db.commit()


async def delete_chat(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
        await db.execute("DELETE FROM chats WHERE id=?", (chat_id,))
        await db.commit()


async def add_message(chat_id: int, role: str, content: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO messages(chat_id, role, content, created_at) VALUES(?, ?, ?, ?)",
            (chat_id, role, content, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()


async def get_messages(chat_id: int, limit: int = MAX_HISTORY):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT role, content FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ) as cur:
            rows = await cur.fetchall()
            return [{"role": r, "content": c} for r, c in reversed(rows)]


async def count_messages(chat_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id=?", (chat_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def get_last_assistant_message(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT content, created_at FROM messages "
            "WHERE chat_id=? AND role='assistant' "
            "ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ) as cur:
            row = await cur.fetchone()
            return row if row else None


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Начать новый чат", callback_data="new_chat")],
        [InlineKeyboardButton(text="📚 История чатов",   callback_data="list_chats")],
    ])


def chat_list_kb(chats) -> InlineKeyboardMarkup:
    buttons = []
    for cid, title, chat_type in chats:
        icon = "🎭" if chat_type == "rp" else "💬"
        buttons.append([
            InlineKeyboardButton(
                text=f"{icon} {title[:38]}",
                callback_data=f"open_chat:{cid}",
            )
        ])
    buttons.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def chat_actions_kb(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📄 Последнее сообщение ИИ",
            callback_data=f"last_ai:{chat_id}",
        )],
        [InlineKeyboardButton(
            text="🗑 Удалить чат",
            callback_data=f"del_chat:{chat_id}",
        )],
        [InlineKeyboardButton(
            text="⬅️ К списку чатов",
            callback_data="list_chats",
        )],
    ])


def need_search(text: str) -> bool:
    low = text.lower()
    return any(t in low for t in SEARCH_TRIGGERS)


async def encode_photo_to_base64(message: Message) -> str:
    photo = message.photo[-1]
    buf = io.BytesIO()
    await bot.download(photo, destination=buf)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def make_title(text: str) -> str:
    t = text.strip().replace("\n", " ")
    return (t[:40] + "…") if len(t) > 40 else t


@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        f"👋 Привет, {message.from_user.first_name}!\n\n"
        f"Я — <b>{BOT_NAME}</b> на модели <b>{MODEL_NAME}</b>.\n"
        f"Каждый чат у меня отдельный — можно обсуждать политику в одном, "
        f"а болтать о жизни в другом, и они не пересекаются.\n\n"
        f"Выбери действие:",
        reply_markup=main_menu_kb(),
        parse_mode="HTML",
    )


@dp.message(Command("new"))
async def cmd_new(message: Message):
    await create_new_chat(message.from_user.id, message)


@dp.message(Command("chats"))
async def cmd_chats(message: Message):
    await show_chats(message.from_user.id, message)


@dp.message(Command("menu"))
async def cmd_menu(message: Message):
    await message.answer("Главное меню:", reply_markup=main_menu_kb())


@dp.message(Command("search"))
async def cmd_search(message: Message):
    query = message.text.replace("/search", "", 1).strip()
    if not query:
        await message.answer("Напиши так: /search твой вопрос")
        return
    await handle_search(message, query)


@dp.message(Command("rp"))
async def cmd_rp(message: Message):
    user_id = message.from_user.id
    chat_id = await create_chat(user_id, "🎭 Ролевая игра", chat_type="rp")
    await set_current_chat(user_id, chat_id)

    await message.answer(
        "🎭 <b>Ролевая игра создана!</b>\n\n"
        "Опиши сценарий и персонажей — я подхвачу и поведу сцену.\n\n"
        "<b>Совет:</b> чем подробнее опишешь обстановку, характер персонажа "
        "и что происходит — тем лучше пойдёт игра.\n\n"
        "Чтобы выйти из игры — просто напиши «выходим из игры» или создай "
        "новый чат через /new.\n\n"
        "<i>Этот чат отдельный — обычные диалоги и другие ролевые игры "
        "его не касаются.</i>",
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "back_to_menu")
async def cb_back_to_menu(call: CallbackQuery):
    await call.message.edit_text(
        f"👋 Я — <b>{BOT_NAME}</b> на модели <b>{MODEL_NAME}</b>.\n\nВыбери действие:",
        reply_markup=main_menu_kb(),
        parse_mode="HTML",
    )
    await call.answer()


@dp.callback_query(F.data == "new_chat")
async def cb_new_chat(call: CallbackQuery):
    await create_new_chat(call.from_user.id, call)
    await call.answer("Новый чат создан")


async def create_new_chat(user_id: int, event):
    chat_id = await create_chat(user_id, "Новый чат", chat_type="normal")
    await set_current_chat(user_id, chat_id)
    text = (
        f"🆕 <b>Новый чат создан</b>\n\n"
        f"Просто пиши сюда что угодно — я запомню контекст именно этого чата. "
        f"Первое сообщение станет названием чата.\n\n"
        f"Команды в чате:\n"
        f"/new — новый чат\n"
        f"/chats — список чатов\n"
        f"/menu — главное меню"
    )
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, parse_mode="HTML")
    else:
        await event.answer(text, parse_mode="HTML")


@dp.callback_query(F.data == "list_chats")
async def cb_list_chats(call: CallbackQuery):
    await show_chats(call.from_user.id, call)
    await call.answer()


async def show_chats(user_id: int, event):
    chats = await list_chats(user_id)
    if not chats:
        text = "📭 У тебя пока нет чатов. Нажми «Начать новый чат»."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Начать новый чат", callback_data="new_chat")],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="back_to_menu")],
        ])
    else:
        text = f"📚 <b>Твои чаты</b> ({len(chats)}):\n\nВыбери, чтобы открыть:"
        kb = chat_list_kb(chats)

    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await event.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data.startswith("open_chat:"))
async def cb_open_chat(call: CallbackQuery):
    chat_id = int(call.data.split(":", 1)[1])
    chat = await get_chat(chat_id)
    if not chat or chat[1] != call.from_user.id:
        await call.answer("Чат не найден", show_alert=True)
        return

    await set_current_chat(call.from_user.id, chat_id)
    cnt = await count_messages(chat_id)
    icon = "🎭" if chat[3] == "rp" else "✅"
    text = (
        f"{icon} <b>Открыт чат:</b> {chat[2]}\n"
        f"Сообщений в памяти: {cnt}\n\n"
        f"Пиши сюда — я отвечу в контексте <i>этого</i> чата.\n\n"
        f"Хочешь вспомнить, на чём остановились — нажми "
        f"«📄 Последнее сообщение ИИ»."
    )
    await call.message.edit_text(text, reply_markup=chat_actions_kb(chat_id), parse_mode="HTML")
    await call.answer()


@dp.callback_query(F.data.startswith("last_ai:"))
async def cb_last_ai(call: CallbackQuery):
    chat_id = int(call.data.split(":", 1)[1])
    chat = await get_chat(chat_id)
    if not chat or chat[1] != call.from_user.id:
        await call.answer("Чат не найден", show_alert=True)
        return

    last = await get_last_assistant_message(chat_id)
    if not last:
        await call.answer("В этом чате ещё нет ответов ИИ", show_alert=True)
        return

    content, created_at = last
    if len(content) > 3500:
        content = content[:3500] + "\n\n… (сообщение обрезано)"

    header = f"📄 <b>Последнее сообщение ИИ в чате:</b>\n<i>{chat[2]}</i>\n\n"
    footer = f"\n\n<i>— {created_at[:19].replace('T', ' ')} UTC</i>"

    await call.message.answer(
        header + content + footer,
        parse_mode="HTML",
    )
    await call.answer()


@dp.callback_query(F.data.startswith("del_chat:"))
async def cb_del_chat(call: CallbackQuery):
    chat_id = int(call.data.split(":", 1)[1])
    chat = await get_chat(chat_id)
    if not chat or chat[1] != call.from_user.id:
        await call.answer("Чат не найден", show_alert=True)
        return

    await delete_chat(chat_id)
    current = await get_current_chat(call.from_user.id)
    if current == chat_id:
        await set_current_chat(call.from_user.id, 0)

    await call.message.edit_text(
        "🗑 Чат удалён.\n\nВыбери действие:",
        reply_markup=main_menu_kb(),
    )
    await call.answer("Удалено")


async def ensure_current_chat(user_id: int) -> int:
    chat_id = await get_current_chat(user_id)
    if not chat_id:
        chat_id = await create_chat(user_id, "Новый чат", chat_type="normal")
        await set_current_chat(user_id, chat_id)
        return chat_id
    chat = await get_chat(chat_id)
    if not chat:
        chat_id = await create_chat(user_id, "Новый чат", chat_type="normal")
        await set_current_chat(user_id, chat_id)
    return chat_id


async def maybe_set_title(chat_id: int, user_text: str):
    cnt = await count_messages(chat_id)
    if cnt <= 1:
        chat = await get_chat(chat_id)
        if chat and chat[3] == "rp":
            await rename_chat(chat_id, f"🎭 {make_title(user_text)}")
        else:
            await rename_chat(chat_id, make_title(user_text))


async def handle_search(message: Message, query: str):
    user_id = message.from_user.id
    chat_id = await ensure_current_chat(user_id)

    await add_message(chat_id, "user", query)
    await maybe_set_title(chat_id, query)

    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    try:
        history = await get_messages(chat_id, MAX_HISTORY)
        payload = [{"role": "system", "content": SYSTEM_PROMPT}] + history

        response = await client.chat.completions.create(
            model=FREE_ROUTER,
            messages=payload,
            temperature=0.7,
            tools=[{"type": "openrouter:web_search"}],
        )
        answer = response.choices[0].message.content
        await add_message(chat_id, "assistant", answer)
        await message.answer(answer)

    except Exception as e:
        logging.exception("Ошибка поиска: %s", e)
        await message.answer("😔 Не получилось выполнить поиск. Попробуй ещё раз.")


@dp.message(F.photo)
async def handle_photo(message: Message):
    user_id = message.from_user.id
    chat_id = await ensure_current_chat(user_id)

    caption = message.caption or "Что изображено на этой фотографии?"

    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    try:
        image_b64 = await encode_photo_to_base64(message)

        await add_message(chat_id, "user", f"[Фото] {caption}")
        await maybe_set_title(chat_id, f"[Фото] {caption}")

        history = await get_messages(chat_id, MAX_HISTORY)
        history = history[:-1]

        vision_content = [
            {"type": "text", "text": caption},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
            },
        ]

        payload = (
            [{"role": "system", "content": SYSTEM_PROMPT}]
            + history
            + [{"role": "user", "content": vision_content}]
        )

        response = await client.chat.completions.create(
            model=FREE_ROUTER,
            messages=payload,
            temperature=0.8,
        )
        answer = response.choices[0].message.content
        used = getattr(response, "model", "unknown")
        logging.info("Фото обработала модель: %s", used)

        await add_message(chat_id, "assistant", answer)
        await message.answer(answer)

    except Exception as e:
        logging.exception("Ошибка обработки фото: %s", e)
        await message.answer("😔 Не получилось обработать фото. Попробуй ещё раз.")


@dp.message(F.text)
async def handle_text(message: Message):
    user_id = message.from_user.id
    chat_id = await ensure_current_chat(user_id)

    text = message.text.strip()
    if not text:
        return

    chat = await get_chat(chat_id)
    is_rp = chat and chat[3] == "rp"

    if not is_rp and need_search(text) and not text.startswith("/"):
        await handle_search(message, text)
        return

    await add_message(chat_id, "user", text)
    await maybe_set_title(chat_id, text)

    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    try:
        history = await get_messages(chat_id, MAX_HISTORY)

        if is_rp:
            system_prompt = RP_SYSTEM_PROMPT
            model = RP_MODEL
            temperature = 0.9
        else:
            system_prompt = SYSTEM_PROMPT
            model = FREE_ROUTER
            temperature = 0.8

        payload = [{"role": "system", "content": system_prompt}] + history

        response = await client.chat.completions.create(
            model=model,
            messages=payload,
            temperature=temperature,
        )
        answer = response.choices[0].message.content
        await add_message(chat_id, "assistant", answer)
        await message.answer(answer)

    except Exception as e:
        logging.exception("Ошибка OpenRouter: %s", e)
        await message.answer("😔 Не получилось ответить. Попробуй ещё раз.")


async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()

    await bot.set_my_commands([
        BotCommand(command="start",  description="Главное меню"),
        BotCommand(command="new",    description="Новый чат"),
        BotCommand(command="chats",  description="История чатов"),
        BotCommand(command="menu",   description="Меню"),
        BotCommand(command="search", description="Поиск в интернете"),
    ])

    threading.Thread(target=run_flask, daemon=True).start()
    print(f"Бот {BOT_NAME} ({MODEL_NAME}) запущен...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())