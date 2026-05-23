"""
🎯 Трекер Привычек — Telegram Bot
Деплой: Render.com (Docker + PostgreSQL)
Переменные: BOT_TOKEN, DATABASE_URL
"""

import os
import asyncio
import hashlib
import hmac
import logging
import secrets
from datetime import datetime, date
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
)
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ─── Config ───────────────────────────────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
TZ = os.getenv("TZ", "Asia/Aqtobe")

# Если оба заданы — на старте создаётся (или обновляется) аккаунт владельца.
# Иначе можно просто зарегистрироваться на сайте.
_web_uid = os.getenv("WEB_USER_ID", "").strip()
WEB_USER_ID: Optional[int] = int(_web_uid) if _web_uid else None
WEB_PASSWORD: Optional[str] = (os.getenv("WEB_PASSWORD") or "").strip() or None

STATIC_DIR = Path(__file__).parent / "static"

# Напоминания по умолчанию (меняются на сайте, хранятся в БД)
DEFAULT_REMINDER = {
    "reminder_enabled": True,
    "reminder_start_hour": 21,
    "reminder_end_hour": 24,
    "reminder_interval_minutes": 2,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("habit_bot")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

scheduler = AsyncIOScheduler(timezone=TZ)
user_states = {}
pool: asyncpg.Pool = None


# ═══════════════════════════════════════════════════════════════════
#  DATABASE (PostgreSQL)
# ═══════════════════════════════════════════════════════════════════

async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS habits (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                name TEXT NOT NULL,
                target INTEGER NOT NULL DEFAULT 1,
                initial_target INTEGER NOT NULL DEFAULT 1,
                cycle_days INTEGER NOT NULL DEFAULT 10,
                step INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT TRUE
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS check_ins (
                id SERIAL PRIMARY KEY,
                habit_id INTEGER NOT NULL REFERENCES habits(id),
                user_id BIGINT NOT NULL,
                check_date TEXT NOT NULL,
                status TEXT NOT NULL,
                UNIQUE(habit_id, check_date)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id BIGINT PRIMARY KEY,
                reminder_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                reminder_start_hour INTEGER NOT NULL DEFAULT 21,
                reminder_end_hour INTEGER NOT NULL DEFAULT 24,
                reminder_interval_minutes INTEGER NOT NULL DEFAULT 2
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_accounts (
                user_id BIGINT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS app_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
    await _ensure_secret_key()
    logger.info("DB ready")


async def get_user_habits(user_id: int) -> list:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM habits WHERE user_id = $1 AND is_active = TRUE ORDER BY id",
            user_id
        )
        return [dict(r) for r in rows]


async def add_habit(user_id: int, name: str, target: int, cycle_days: int, step: int) -> int:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO habits (user_id, name, target, initial_target, cycle_days, step, created_at)
               VALUES ($1, $2, $3, $3, $4, $5, $6) RETURNING id""",
            user_id, name, target, cycle_days, step, datetime.now(ZoneInfo(TZ)).date().isoformat()
        )
        return row["id"]


async def delete_habit(habit_id: int, user_id: int):
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE habits SET is_active = FALSE WHERE id = $1 AND user_id = $2",
            habit_id, user_id
        )


async def get_today_checkins(user_id: int) -> dict:
    today = datetime.now(ZoneInfo(TZ)).date().isoformat()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT habit_id, status FROM check_ins WHERE user_id = $1 AND check_date = $2",
            user_id, today
        )
        return {r["habit_id"]: r["status"] for r in rows}


async def set_checkin(habit_id: int, user_id: int, status: str):
    today = datetime.now(ZoneInfo(TZ)).date().isoformat()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO check_ins (habit_id, user_id, check_date, status)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT(habit_id, check_date) DO UPDATE SET status = $4""",
            habit_id, user_id, today, status
        )


async def delete_checkin(habit_id: int, user_id: int):
    today = datetime.now(ZoneInfo(TZ)).date().isoformat()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM check_ins WHERE habit_id = $1 AND user_id = $2 AND check_date = $3",
            habit_id, user_id, today
        )


async def get_habit_by_id(habit_id: int) -> Optional[dict]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM habits WHERE id = $1", habit_id)
        return dict(row) if row else None


async def get_habit_stats(habit_id: int) -> dict:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT status, COUNT(*) as cnt FROM check_ins WHERE habit_id = $1 GROUP BY status",
            habit_id
        )
        stats = {"done": 0, "not_done": 0, "skip": 0}
        for r in rows:
            stats[r["status"]] = r["cnt"]
        return stats


async def get_all_stats(user_id: int) -> list:
    habits = await get_user_habits(user_id)
    result = []
    for h in habits:
        stats = await get_habit_stats(h["id"])
        total = stats["done"] + stats["not_done"] + stats["skip"]
        streak = await get_streak(h["id"])
        result.append({
            "id": h["id"],
            "name": h["name"],
            "target": compute_current_target(h),
            "done": stats["done"],
            "not_done": stats["not_done"],
            "skip": stats["skip"],
            "total": total,
            "rate": round(stats["done"] / total * 100) if total > 0 else 0,
            "streak": streak,
        })
    return result


async def get_streak(habit_id: int) -> int:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT status FROM check_ins WHERE habit_id = $1 ORDER BY check_date DESC",
            habit_id
        )
    streak = 0
    for r in rows:
        if r["status"] == "done":
            streak += 1
        else:
            break
    return streak


async def get_all_user_ids() -> list:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT user_id FROM habits WHERE is_active = TRUE"
        )
        return [r["user_id"] for r in rows]


async def get_settings(user_id: int) -> dict:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM user_settings WHERE user_id = $1", user_id
        )
    if row:
        return dict(row)
    return {"user_id": user_id, **DEFAULT_REMINDER}


async def update_settings(user_id: int, enabled: bool, start_hour: int,
                          end_hour: int, interval: int):
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO user_settings
                   (user_id, reminder_enabled, reminder_start_hour,
                    reminder_end_hour, reminder_interval_minutes)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (user_id) DO UPDATE SET
                   reminder_enabled = $2,
                   reminder_start_hour = $3,
                   reminder_end_hour = $4,
                   reminder_interval_minutes = $5""",
            user_id, enabled, start_hour, end_hour, interval
        )


# ═══════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════

def local_today() -> date:
    return datetime.now(ZoneInfo(TZ)).date()


def compute_current_target(habit: dict) -> int:
    if habit["step"] == 0:
        return habit["target"]
    created = date.fromisoformat(habit["created_at"])
    days_passed = (local_today() - created).days
    cycles_completed = days_passed // habit["cycle_days"]
    return habit["initial_target"] + (habit["step"] * cycles_completed)


def get_day_in_cycle(habit: dict) -> str:
    created = date.fromisoformat(habit["created_at"])
    days_passed = (local_today() - created).days
    day_in_cycle = (days_passed % habit["cycle_days"]) + 1
    return f"{day_in_cycle}/{habit['cycle_days']}"


def progress_bar(percent: int, length: int = 10) -> str:
    filled = round(percent / 100 * length)
    return "▓" * filled + "░" * (length - filled)


# ═══════════════════════════════════════════════════════════════════
#  KEYBOARDS
# ═══════════════════════════════════════════════════════════════════

def main_menu_kb(has_habits: bool = True) -> InlineKeyboardMarkup:
    rows = []
    if has_habits:
        rows.append([InlineKeyboardButton(text="✍️ Отметить привычки", callback_data="checkin_start")])
        rows.append([
            InlineKeyboardButton(text="📊 Статистика", callback_data="stats"),
            InlineKeyboardButton(text="📋 Мои привычки", callback_data="my_habits"),
        ])
    rows.append([InlineKeyboardButton(text="➕ Новая привычка", callback_data="add_habit")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def checkin_kb(habit_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да", callback_data=f"done_{habit_id}"),
            InlineKeyboardButton(text="❌ Нет", callback_data=f"notdone_{habit_id}"),
            InlineKeyboardButton(text="⏭ Скип", callback_data=f"skip_{habit_id}"),
        ],
        [InlineKeyboardButton(text="◀️ Меню", callback_data="menu")],
    ])


def progression_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📈 Да", callback_data="prog_yes"),
            InlineKeyboardButton(text="➡️ Нет", callback_data="prog_no"),
        ],
        [InlineKeyboardButton(text="✖️ Отмена", callback_data="cancel")],
    ])


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✖️ Отмена", callback_data="cancel")],
    ])


def back_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Меню", callback_data="menu")],
    ])


async def habits_list_kb(user_id: int) -> InlineKeyboardMarkup:
    habits = await get_user_habits(user_id)
    rows = []
    for h in habits:
        rows.append([InlineKeyboardButton(
            text=f"🗑 Удалить «{h['name']}»",
            callback_data=f"del_{h['id']}"
        )])
    rows.append([InlineKeyboardButton(text="◀️ Меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ═══════════════════════════════════════════════════════════════════
#  MESSAGE BUILDERS
# ═══════════════════════════════════════════════════════════════════

async def build_main(user_id: int) -> str:
    habits = await get_user_habits(user_id)
    checkins = await get_today_checkins(user_id)
    total = len(habits)
    done_count = sum(1 for h in habits if h["id"] in checkins)
    today_str = date.today().strftime("%d.%m.%Y")

    status_map = {"done": "✅", "not_done": "❌", "skip": "⏭"}

    lines = [
        "🎯 <b>Трекер Привычек</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"📅 {today_str}  ·  Отмечено: <b>{done_count}/{total}</b>",
        "",
    ]

    if not habits:
        lines.append("🫙 <i>Пусто! Нажми ➕ и добавь первую привычку</i>")
    else:
        for h in habits:
            ct = compute_current_target(h)
            icon = status_map.get(checkins.get(h["id"], ""), "⬜")
            lines.append(f"  {icon}  {h['name']}  ·  🎯 {ct}")

    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


async def build_habits_detail(user_id: int) -> str:
    habits = await get_user_habits(user_id)
    if not habits:
        return "📋 <b>Мои привычки</b>\n\n🫙 <i>Список пуст</i>"

    lines = ["📋 <b>Мои привычки</b>", ""]
    for h in habits:
        ct = compute_current_target(h)
        day = get_day_in_cycle(h)
        step = f" (+{h['step']})" if h["step"] > 0 else ""
        lines.append(f"▸ <b>{h['name']}</b>  🎯 {ct}{step}")
        lines.append(f"   📆 день {day}  ·  с {h['created_at']}")
        lines.append("")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
#  HANDLERS
# ═══════════════════════════════════════════════════════════════════

@router.message(Command("start", "menu"))
async def cmd_start(message: Message):
    user_states.pop(message.from_user.id, None)
    habits = await get_user_habits(message.from_user.id)
    text = await build_main(message.from_user.id)
    await message.answer(text, reply_markup=main_menu_kb(bool(habits)))


@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery):
    user_states.pop(cb.from_user.id, None)
    habits = await get_user_habits(cb.from_user.id)
    text = await build_main(cb.from_user.id)
    await cb.message.edit_text(text, reply_markup=main_menu_kb(bool(habits)))
    await cb.answer()


@router.callback_query(F.data == "cancel")
async def cb_cancel(cb: CallbackQuery):
    user_states.pop(cb.from_user.id, None)
    habits = await get_user_habits(cb.from_user.id)
    text = await build_main(cb.from_user.id)
    await cb.message.edit_text(text, reply_markup=main_menu_kb(bool(habits)))
    await cb.answer("✖️ Отменено")


# ─── Add Habit ────────────────────────────────────────────────────

@router.callback_query(F.data == "add_habit")
async def cb_add(cb: CallbackQuery):
    user_states[cb.from_user.id] = {"state": "name", "data": {}}
    await cb.message.edit_text(
        "✏️ <b>Новая привычка</b>\n\nВведи название:",
        reply_markup=cancel_kb()
    )
    await cb.answer()


@router.message(lambda m: user_states.get(m.from_user.id, {}).get("state") == "name")
async def on_name(msg: Message):
    uid = msg.from_user.id
    name = msg.text.strip()
    if not name or len(name) > 64:
        return await msg.answer("⚠️ Название: 1-64 символа. Попробуй:")

    user_states[uid]["data"]["name"] = name
    user_states[uid]["state"] = "progression"

    await msg.answer(
        "📈 <b>Прогрессия</b>\n\n"
        "Добавить числовую цель с автоматическим увеличением?\n"
        "<i>Например: 20 отжиманий, +2 каждые 10 дней</i>",
        reply_markup=progression_kb()
    )


@router.callback_query(F.data == "prog_no")
async def cb_prog_no(cb: CallbackQuery):
    uid = cb.from_user.id
    st = user_states.get(uid, {})
    if st.get("state") != "progression":
        return await cb.answer("⚠️ Нет активного действия")

    name = st["data"]["name"]
    await add_habit(uid, name, target=1, cycle_days=10, step=0)
    user_states.pop(uid, None)

    habits = await get_user_habits(uid)
    await cb.message.edit_text(
        f"✅ Привычка «<b>{name}</b>» добавлена!\n\n"
        f"Без прогрессии — просто отмечай каждый день 💪",
        reply_markup=main_menu_kb(bool(habits))
    )
    await cb.answer("✅ Добавлено!")


@router.callback_query(F.data == "prog_yes")
async def cb_prog_yes(cb: CallbackQuery):
    uid = cb.from_user.id
    st = user_states.get(uid, {})
    if st.get("state") != "progression":
        return await cb.answer("⚠️ Нет активного действия")

    user_states[uid]["state"] = "target"
    await cb.message.edit_text(
        "🔢 <b>Начальная цель</b>\n\n"
        "Введи начальное количество (число):\n"
        "<i>Например: 20</i>",
        reply_markup=cancel_kb()
    )
    await cb.answer()


@router.message(lambda m: user_states.get(m.from_user.id, {}).get("state") == "target")
async def on_target(msg: Message):
    uid = msg.from_user.id
    try:
        val = int(msg.text.strip())
        assert val > 0
    except (ValueError, AssertionError):
        return await msg.answer("⚠️ Введи положительное целое число:")

    user_states[uid]["data"]["target"] = val
    user_states[uid]["state"] = "cycle"
    await msg.answer(
        "🗓 <b>Длина цикла</b>\n\n"
        "Через сколько дней увеличивать цель?\n"
        "<i>Например: 10</i>",
        reply_markup=cancel_kb()
    )


@router.message(lambda m: user_states.get(m.from_user.id, {}).get("state") == "cycle")
async def on_cycle(msg: Message):
    uid = msg.from_user.id
    try:
        val = int(msg.text.strip())
        assert val > 0
    except (ValueError, AssertionError):
        return await msg.answer("⚠️ Введи положительное целое число:")

    user_states[uid]["data"]["cycle_days"] = val
    user_states[uid]["state"] = "step"
    await msg.answer(
        "📈 <b>Шаг прогрессии</b>\n\n"
        "На сколько увеличивать цель каждый цикл?\n"
        "<i>Например: 2</i>",
        reply_markup=cancel_kb()
    )


@router.message(lambda m: user_states.get(m.from_user.id, {}).get("state") == "step")
async def on_step(msg: Message):
    uid = msg.from_user.id
    try:
        val = int(msg.text.strip())
        assert val >= 0
    except (ValueError, AssertionError):
        return await msg.answer("⚠️ Введи положительное целое число:")

    d = user_states[uid]["data"]
    await add_habit(uid, d["name"], d["target"], d["cycle_days"], val)
    user_states.pop(uid, None)

    habits = await get_user_habits(uid)
    await msg.answer(
        f"✅ Привычка «<b>{d['name']}</b>» добавлена!\n\n"
        f"🎯 Цель: {d['target']}\n"
        f"🗓 Цикл: {d['cycle_days']} дней\n"
        f"📈 Шаг: +{val} каждый цикл",
        reply_markup=main_menu_kb(bool(habits))
    )


# ─── Check-in ────────────────────────────────────────────────────

@router.callback_query(F.data == "checkin_start")
async def cb_checkin(cb: CallbackQuery):
    await _show_next_checkin(cb)


import random

PRAISE = [
    "Молодец! 💪🔥",
    "Красавчик! Так держать! 🏆",
    "Огонь! Ты машина! 🚀",
    "Шикарно! Ещё один шаг вперёд! 🎯",
    "Супер! Ты на верном пути! ⭐",
    "Бомба! Продолжай в том же духе! 💣",
    "Легенда! Ни дня без привычки! 👑",
    "Мощно! Дисциплина — сила! 🦾",
    "Респект! Ты не сдаёшься! 🫡",
    "Космос! Привычка закрепляется! 🌟",
]


@router.callback_query(F.data.startswith("done_"))
async def cb_done(cb: CallbackQuery):
    hid = int(cb.data.split("_", 1)[1])
    await set_checkin(hid, cb.from_user.id, "done")
    await cb.answer(f"✅ {random.choice(PRAISE)}", show_alert=True)
    await _show_next_checkin(cb)


@router.callback_query(F.data.startswith("notdone_"))
async def cb_notdone(cb: CallbackQuery):
    hid = int(cb.data.split("_", 1)[1])
    await set_checkin(hid, cb.from_user.id, "not_done")
    await cb.answer("❌ Не сделано")
    await _show_next_checkin(cb)


@router.callback_query(F.data.startswith("skip_"))
async def cb_skip(cb: CallbackQuery):
    hid = int(cb.data.split("_", 1)[1])
    await set_checkin(hid, cb.from_user.id, "skip")
    await cb.answer("⏭ Пропущено")
    await _show_next_checkin(cb)


async def _show_next_checkin(cb: CallbackQuery):
    uid = cb.from_user.id
    habits = await get_user_habits(uid)
    checkins = await get_today_checkins(uid)
    unchecked = [h for h in habits if h["id"] not in checkins]

    if not unchecked:
        await cb.message.edit_text(
            f"🎉 <b>Все {len(habits)} привычек отмечены!</b>\n\n"
            f"Ты огонь! 🔥🔥🔥",
            reply_markup=main_menu_kb(True)
        )
        return

    h = unchecked[0]
    ct = compute_current_target(h)
    day = get_day_in_cycle(h)

    await cb.message.edit_text(
        f"📋 <b>Отметка</b>  ({len(unchecked)} осталось)\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"❓ <b>{h['name']}</b>\n"
        f"🎯 Цель: {ct}  ·  📆 День {day}\n\n"
        f"Выполнена сегодня?",
        reply_markup=checkin_kb(h["id"])
    )


# ─── My Habits ────────────────────────────────────────────────────

@router.callback_query(F.data == "my_habits")
async def cb_my_habits(cb: CallbackQuery):
    text = await build_habits_detail(cb.from_user.id)
    kb = await habits_list_kb(cb.from_user.id)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("del_"))
async def cb_del(cb: CallbackQuery):
    hid = int(cb.data.split("_", 1)[1])
    habit = await get_habit_by_id(hid)
    if habit and habit["user_id"] == cb.from_user.id:
        await delete_habit(hid, cb.from_user.id)
        await cb.answer(f"🗑 «{habit['name']}» удалена")
    else:
        await cb.answer("⚠️ Не найдена")

    text = await build_habits_detail(cb.from_user.id)
    kb = await habits_list_kb(cb.from_user.id)
    await cb.message.edit_text(text, reply_markup=kb)


# ─── Statistics ──────────────────────────────────────────────────

@router.callback_query(F.data == "stats")
async def cb_stats(cb: CallbackQuery):
    stats = await get_all_stats(cb.from_user.id)

    if not stats:
        text = "📊 <b>Статистика</b>\n\n🫙 <i>Нет данных</i>"
    else:
        lines = ["📊 <b>Статистика</b>", ""]
        for s in stats:
            bar = progress_bar(s["rate"])
            fire = "🔥" if s["streak"] >= 3 else ""
            lines.append(f"▸ <b>{s['name']}</b>  🎯 {s['target']}")
            lines.append(f"   {bar}  {s['rate']}%")
            lines.append(f"   ✅ {s['done']}  ❌ {s['not_done']}  ⏭ {s['skip']}  🔗 {s['streak']}д {fire}")
            lines.append("")
        text = "\n".join(lines)

    await cb.message.edit_text(text, reply_markup=back_menu_kb())
    await cb.answer()


# ═══════════════════════════════════════════════════════════════════
#  SPAM REMINDERS  21:00 - 00:00 каждые 2 мин
# ═══════════════════════════════════════════════════════════════════

REMINDER_MESSAGES = [
    "🔔 Эй! Неотмеченные привычки! Не ленись 💪",
    "⏰ Тик-так! Привычки ждут! 🚀",
    "😤 Отметь привычки уже! 🔥",
    "🫵 Привычки сами себя не отметят!",
    "💀 Полночь близко… Отмечай!",
    "🚨 ТРЕВОГА! Неотмеченные привычки! 🚨",
    "😈 Буду спамить пока не отметишь.",
    "🐌 Даже улитка быстрее…",
    "⚡️ До полуночи мало. ДЕЙСТВУЙ!",
    "🫠 Каждые 2 мин. Пока. Не. Отметишь.",
]

_reminder_counter = {}


async def send_reminders():
    """Запускается раз в минуту. Окно и интервал — индивидуальные для пользователя."""
    tz = ZoneInfo(TZ)
    now = datetime.now(tz)

    user_ids = await get_all_user_ids()

    for user_id in user_ids:
        try:
            st = await get_settings(user_id)
            if not st["reminder_enabled"]:
                continue

            start = st["reminder_start_hour"]
            end = st["reminder_end_hour"]
            interval = max(1, st["reminder_interval_minutes"])

            # Вне окна напоминаний
            if now.hour < start or now.hour >= end:
                _reminder_counter.pop(user_id, None)
                continue

            # Шлём только раз в `interval` минут от начала окна
            minutes_into_window = (now.hour - start) * 60 + now.minute
            if minutes_into_window % interval != 0:
                continue

            habits = await get_user_habits(user_id)
            checkins = await get_today_checkins(user_id)
            unchecked = [h for h in habits if h["id"] not in checkins]

            if not unchecked:
                _reminder_counter.pop(user_id, None)
                continue

            idx = _reminder_counter.get(user_id, 0) % len(REMINDER_MESSAGES)
            _reminder_counter[user_id] = idx + 1

            names = "\n".join(f"  ▸ {h['name']}" for h in unchecked)
            mins_left = (end * 60 - now.hour * 60 - now.minute)

            text = (
                f"{REMINDER_MESSAGES[idx]}\n\n"
                f"📌 <b>Не отмечено ({len(unchecked)}):</b>\n{names}\n\n"
                f"⏳ Осталось времени: ~{mins_left} мин"
            )

            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✍️ Отметить!", callback_data="checkin_start")],
            ])
            await bot.send_message(user_id, text, reply_markup=kb)

        except Exception as e:
            logger.warning(f"Reminder fail {user_id}: {e}")


# ═══════════════════════════════════════════════════════════════════
#  FALLBACK
# ═══════════════════════════════════════════════════════════════════

@router.message()
async def fallback(message: Message):
    if message.from_user.id in user_states:
        return
    habits = await get_user_habits(message.from_user.id)
    await message.answer(
        "🤔 Используй кнопки или /start",
        reply_markup=main_menu_kb(bool(habits))
    )


# ═══════════════════════════════════════════════════════════════════
#  HEALTH + MAIN
# ═══════════════════════════════════════════════════════════════════

WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг",
               "пятница", "суббота", "воскресенье"]


@web.middleware
async def error_middleware(request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception as e:
        logger.exception("Web request failed")
        return web.json_response({"error": str(e)}, status=500)


SECRET_KEY: bytes = b""


async def _ensure_secret_key():
    """Загружает или генерирует секрет для подписи сессий (хранится в БД)."""
    global SECRET_KEY
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value FROM app_meta WHERE key = 'secret_key'"
        )
        if row:
            SECRET_KEY = bytes.fromhex(row["value"])
            return
        SECRET_KEY = secrets.token_bytes(32)
        await conn.execute(
            "INSERT INTO app_meta (key, value) VALUES ('secret_key', $1) "
            "ON CONFLICT (key) DO NOTHING",
            SECRET_KEY.hex()
        )


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$", 1)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, AttributeError):
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return hmac.compare_digest(digest, expected)


async def get_account(user_id: int) -> Optional[dict]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM user_accounts WHERE user_id = $1", user_id
        )
    return dict(row) if row else None


async def create_account(user_id: int, password: str):
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO user_accounts (user_id, password_hash, created_at)
               VALUES ($1, $2, $3)""",
            user_id, hash_password(password),
            datetime.now(ZoneInfo(TZ)).date().isoformat()
        )


async def upsert_account(user_id: int, password: str):
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO user_accounts (user_id, password_hash, created_at)
               VALUES ($1, $2, $3)
               ON CONFLICT (user_id) DO UPDATE SET password_hash = $2""",
            user_id, hash_password(password),
            datetime.now(ZoneInfo(TZ)).date().isoformat()
        )


def _session_cookie(user_id: int) -> str:
    tag = hmac.new(SECRET_KEY, str(user_id).encode(),
                   hashlib.sha256).hexdigest()
    return f"{user_id}.{tag}"


def _session_user(request) -> Optional[int]:
    cookie = request.cookies.get("habit_session", "")
    if not cookie or "." not in cookie:
        return None
    try:
        uid_str, tag = cookie.split(".", 1)
        uid = int(uid_str)
    except ValueError:
        return None
    expected = hmac.new(SECRET_KEY, str(uid).encode(),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(tag, expected):
        return None
    return uid


PUBLIC_PATHS = {"/login", "/register", "/logout", "/health"}


@web.middleware
async def auth_middleware(request, handler):
    path = request.path
    if path in PUBLIC_PATHS or path.startswith("/static/"):
        return await handler(request)
    uid = _session_user(request)
    if uid is None:
        if path.startswith("/api/"):
            return web.json_response({"error": "Требуется вход"}, status=401)
        raise web.HTTPFound("/login")
    request["user_id"] = uid
    return await handler(request)


def _set_session(resp, user_id: int):
    resp.set_cookie("habit_session", _session_cookie(user_id),
                    max_age=60 * 60 * 24 * 30, httponly=True, samesite="Lax")


async def page_login(request):
    if _session_user(request) is not None:
        raise web.HTTPFound("/")
    return web.FileResponse(STATIC_DIR / "login.html")


async def page_register(request):
    if _session_user(request) is not None:
        raise web.HTTPFound("/")
    return web.FileResponse(STATIC_DIR / "register.html")


async def api_login(request):
    data = await request.json()
    try:
        uid = int(data.get("user_id"))
    except (TypeError, ValueError):
        return web.json_response(
            {"error": "Введите корректный Telegram ID"}, status=400)
    password = str(data.get("password", ""))
    if not password:
        return web.json_response({"error": "Введите пароль"}, status=400)
    account = await get_account(uid)
    if not account or not verify_password(password, account["password_hash"]):
        return web.json_response(
            {"error": "Неверный Telegram ID или пароль"}, status=401)
    resp = web.json_response({"ok": True})
    _set_session(resp, uid)
    return resp


async def api_register(request):
    data = await request.json()
    try:
        uid = int(data.get("user_id"))
        assert uid > 0
    except (TypeError, ValueError, AssertionError):
        return web.json_response(
            {"error": "Telegram ID — положительное целое число"}, status=400)
    password = str(data.get("password", ""))
    if len(password) < 4:
        return web.json_response(
            {"error": "Пароль не короче 4 символов"}, status=400)
    if await get_account(uid):
        return web.json_response(
            {"error": "Аккаунт с таким ID уже есть — войдите"}, status=409)
    await create_account(uid, password)
    resp = web.json_response({"ok": True})
    _set_session(resp, uid)
    return resp


async def page_logout(request):
    resp = web.HTTPFound("/login")
    resp.del_cookie("habit_session")
    return resp


async def page_index(request):
    return web.FileResponse(STATIC_DIR / "index.html")


async def health(request):
    return web.Response(status=200, text="OK")


async def api_state(request):
    uid = request["user_id"]

    habits = await get_user_habits(uid)
    checkins = await get_today_checkins(uid)
    stats = await get_all_stats(uid)
    settings = await get_settings(uid)
    now = datetime.now(ZoneInfo(TZ))

    habit_list = []
    for h in habits:
        habit_list.append({
            "id": h["id"],
            "name": h["name"],
            "target": compute_current_target(h),
            "step": h["step"],
            "day_in_cycle": get_day_in_cycle(h) if h["step"] > 0 else None,
            "created_at": h["created_at"],
            "status": checkins.get(h["id"]),
        })

    return web.json_response({
        "date": now.strftime("%d.%m.%Y"),
        "weekday": WEEKDAYS_RU[now.weekday()],
        "habits": habit_list,
        "done_today": sum(1 for h in habit_list if h["status"] == "done"),
        "stats": stats,
        "settings": {
            "reminder_enabled": settings["reminder_enabled"],
            "reminder_start_hour": settings["reminder_start_hour"],
            "reminder_end_hour": settings["reminder_end_hour"],
            "reminder_interval_minutes": settings["reminder_interval_minutes"],
        },
    })


async def api_checkin(request):
    uid = request["user_id"]
    data = await request.json()
    try:
        habit_id = int(data["habit_id"])
    except (KeyError, ValueError, TypeError):
        return web.json_response({"error": "Неверный habit_id"}, status=400)
    status = data.get("status")

    habit = await get_habit_by_id(habit_id)
    if not habit or habit["user_id"] != uid:
        return web.json_response({"error": "Привычка не найдена"}, status=404)

    if status == "clear":
        await delete_checkin(habit_id, uid)
    elif status in ("done", "not_done", "skip"):
        await set_checkin(habit_id, uid, status)
    else:
        return web.json_response({"error": "Неверный статус"}, status=400)

    return web.json_response({"ok": True})


async def api_add_habit(request):
    uid = request["user_id"]
    data = await request.json()
    name = (data.get("name") or "").strip()
    if not name or len(name) > 64:
        return web.json_response(
            {"error": "Название должно быть от 1 до 64 символов"}, status=400)

    if data.get("progression"):
        try:
            target = int(data["target"])
            cycle = int(data["cycle_days"])
            step = int(data["step"])
            assert target > 0 and cycle > 0 and step >= 0
        except (KeyError, ValueError, TypeError, AssertionError):
            return web.json_response(
                {"error": "Цель и цикл — целые больше 0, шаг — целое от 0"},
                status=400)
    else:
        target, cycle, step = 1, 10, 0

    hid = await add_habit(uid, name, target, cycle, step)
    return web.json_response({"ok": True, "id": hid})


async def api_delete_habit(request):
    uid = request["user_id"]
    try:
        hid = int(request.match_info["hid"])
    except ValueError:
        return web.json_response({"error": "Неверный id"}, status=400)
    habit = await get_habit_by_id(hid)
    if not habit or habit["user_id"] != uid:
        return web.json_response({"error": "Привычка не найдена"}, status=404)
    await delete_habit(hid, uid)
    return web.json_response({"ok": True})


async def api_save_settings(request):
    uid = request["user_id"]
    data = await request.json()
    try:
        enabled = bool(data["reminder_enabled"])
        start = int(data["reminder_start_hour"])
        end = int(data["reminder_end_hour"])
        interval = int(data["reminder_interval_minutes"])
        assert 0 <= start <= 23
        assert 1 <= end <= 24
        assert end > start
        assert 1 <= interval <= 120
    except (KeyError, ValueError, TypeError, AssertionError):
        return web.json_response(
            {"error": "Начало 0–23, конец 1–24 (позже начала), "
                      "интервал 1–120 минут"},
            status=400)

    await update_settings(uid, enabled, start, end, interval)
    return web.json_response({"ok": True})


async def start_web():
    app = web.Application(middlewares=[error_middleware, auth_middleware])
    app.router.add_get("/", page_index)
    app.router.add_get("/health", health)
    app.router.add_get("/login", page_login)
    app.router.add_post("/login", api_login)
    app.router.add_get("/register", page_register)
    app.router.add_post("/register", api_register)
    app.router.add_get("/logout", page_logout)
    app.router.add_get("/api/state", api_state)
    app.router.add_post("/api/checkin", api_checkin)
    app.router.add_post("/api/habits", api_add_habit)
    app.router.add_delete("/api/habits/{hid}", api_delete_habit)
    app.router.add_post("/api/settings", api_save_settings)
    if STATIC_DIR.is_dir():
        app.router.add_static("/static/", path=str(STATIC_DIR))

    port = int(os.getenv("PORT", 10000))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Web UI on :{port}")


async def main():
    await init_db()

    if WEB_USER_ID is not None and WEB_PASSWORD is not None:
        await upsert_account(WEB_USER_ID, WEB_PASSWORD)
        logger.info(f"Owner account ensured: {WEB_USER_ID}")

    scheduler.add_job(
        send_reminders,
        "interval",
        minutes=1,
        id="spam",
        replace_existing=True,
    )
    scheduler.start()

    await start_web()

    logger.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
