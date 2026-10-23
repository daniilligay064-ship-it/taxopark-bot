# -*- coding: utf-8 -*-
import asyncio
import logging
import os
import re
import sqlite3
import time
from datetime import date

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config import BOT_TOKEN, OWNERS
from database import init
from sheets_helper import get_cars, get_earnings, get_cash, get_balances
from finance import calc_day, calc_period, fmt, parse_date
from ai_helper import ask_ai
from features import check_numeric_grounding
from scheduler import (
    rebuild_fewshot_pool_locked,
    needs_catchup,
    weekly_report_job,
    quality_check_job,
)
import commands

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

from paths import DB_PATH
MONTH_RE = r"январ\w*|феврал\w*|март\w*|апрел\w*|ма[йя]\w*|июн\w*|июл\w*|август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*|месяц\w*"


def classify_question(text):
    q = text.lower().replace("ё", "е")
    if re.search(r"\bтоп\b", q) and re.search(r"машин|автомобил", q):
        return "top_cars"
    if re.search(r"баланс|остаток", q):
        return "balance"
    if re.search(r"касс|приход|расход", q):
        return "cash_summary"
    if re.search(r"сколько.*машин|машин\s+у|автомобил\s+у", q):
        return "cars_by_owner"
    if re.search(r"заработ|доход|выручк", q) and re.search(r"за\s+месяц|" + MONTH_RE + r"|\d{1,2}\.\d{1,2}\.\d{4}|20\d{2}", q):
        return "earnings_by_owner_month"
    return "other"


def semantic_key(question):
    q = question.lower().replace("ё", "е")
    q = re.sub(r"\b(?:александр\w*\s+борисович\w*|анна|даниил\w*|данил\w*|аб)\b", "{owner}", q)
    q = re.sub(r"\b\d{1,2}\.\d{1,2}\.\d{4}\b", "{date}", q)
    q = re.sub(r"\b20\d{2}\b", "{year}", q)
    q = re.sub(r"\b(?:" + MONTH_RE + r")\b", "{period}", q)
    q = re.sub(r"\b\d+\b", "{n}", q)
    q = re.sub(r"\b(?:сколько|покажи|посчитай|рассчитай|пожалуйста)\b", " ", q)
    q = re.sub(r"\b(?:заработ\w*|доход\w*|выручк\w*)\b", "{income}", q)
    q = re.sub(r"[^\w{}]+", " ", q)
    q = " ".join(q.split())
    return classify_question(question) + "|" + q


def normalize_owner_key(owner):
    if not owner:
        return None
    name = " ".join(str(owner).casefold().replace("ё", "е").split())
    return {
        "аб": "ab",
        "александр борисович": "ab",
        "анна": "anna",
        "даниил": "daniil",
        "данил": "daniil",
    }.get(name, name)


def owner_key_from_question(question):
    from ai_helper import find_owner
    return normalize_owner_key(find_owner(question))


def migrate_db():
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(qa_log)")}
        if "owner_key" not in cols:
            conn.execute("ALTER TABLE qa_log ADD COLUMN owner_key TEXT")
        if "hallucination_warning" not in cols:
            conn.execute("ALTER TABLE qa_log ADD COLUMN hallucination_warning INTEGER NOT NULL DEFAULT 0")
        rows = conn.execute(
            "SELECT id, question FROM qa_log WHERE owner_key IS NULL OR TRIM(owner_key)=''"
        ).fetchall()
        for qa_id, q in rows:
            k = owner_key_from_question(q)
            if k:
                conn.execute("UPDATE qa_log SET owner_key=? WHERE id=?", (k, qa_id))
        conn.execute("CREATE INDEX IF NOT EXISTS idx_qa_semantic_owner ON qa_log(semantic_key, owner_key)")


def save_qa_log(user_id, question, q_type, sql, answer, latency_ms):
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        cur = conn.execute(
            "INSERT INTO qa_log (user_id, question, question_type, generated_sql, answer, latency_ms, semantic_key, owner_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, question, q_type, sql, answer, latency_ms,
             semantic_key(question), owner_key_from_question(question))
        )
        return cur.lastrowid


def save_hallucination_flag(qa_id, warning):
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.execute("UPDATE qa_log SET hallucination_warning=? WHERE id=?", (int(warning), qa_id))


def save_feedback(qa_id, voter_id, vote):
    if vote not in (0, 1):
        return False
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        if conn.execute("SELECT 1 FROM qa_log WHERE id = ?", (qa_id,)).fetchone() is None:
            return False
        conn.execute(
            "INSERT INTO qa_feedback (qa_log_id, voter_id, is_correct) VALUES (?, ?, ?) "
            "ON CONFLICT(qa_log_id, voter_id) DO UPDATE SET is_correct = excluded.is_correct, created_at = CURRENT_TIMESTAMP",
            (qa_id, voter_id, vote)
        )
        if vote == 1 and voter_id in OWNERS:
            conn.execute(
                "UPDATE qa_log SET sql_approved=1, corrected_sql=generated_sql "
                "WHERE id=? AND generated_sql IS NOT NULL AND TRIM(generated_sql)<>''",
                (qa_id,)
            )
    return True


class FeedbackCB(CallbackData, prefix="fb"):
    qa_id: int
    vote: int


def feedback_keyboard(qa_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Верно", callback_data=FeedbackCB(qa_id=qa_id, vote=1).pack()),
        InlineKeyboardButton(text="❌ Неверно", callback_data=FeedbackCB(qa_id=qa_id, vote=0).pack()),
    ]])


def is_owner(uid):
    return uid in OWNERS


def main_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="Финансы за последний день", callback_data="today")
    kb.button(text="Финансы за месяц", callback_data="month")
    kb.button(text="Машины", callback_data="cars")
    kb.button(text="Владельцы", callback_data="owners")
    kb.button(text="Касса", callback_data="cash")
    kb.button(text="Баланс", callback_data="balance")
    kb.button(text="Статистика ИИ", callback_data="stats")
    kb.adjust(2)
    return kb.as_markup()


async def send_long(msg, text, reply_markup=None):
    if len(text) <= 3800:
        await msg.answer(text, reply_markup=reply_markup)
        return
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3800:
            parts.append(cur)
            cur = line
        else:
            cur += ("\n" if cur else "") + line
    if cur:
        parts.append(cur)
    for i, p in enumerate(parts):
        if i == len(parts) - 1:
            await msg.answer(p, reply_markup=reply_markup)
        else:
            await msg.answer(p)


@dp.message(CommandStart())
async def start(m: Message):
    if not is_owner(m.from_user.id):
        await m.answer("Доступ закрыт. Ваш ID: " + str(m.from_user.id))
        return
    text = (
        f"Привет, {OWNERS[m.from_user.id]}!\n\n"
        f"Жми кнопки или пиши вопрос текстом — отвечу с помощью ИИ.\n"
        f"Под ответом жми ✅ или ❌ — я буду учиться.\n"
        f"Команды:\n"
        f"  /dashboard — сводка\n"
        f"  /stats — статистика ИИ\n"
        f"  /explain — few-shot пул\n"
        f"  /rebuild — пересобрать пул\n"
        f"  /export earnings 01.10.2026 08.10.2026 — CSV\n"
        f"  /chart daily 01.10.2026 08.10.2026 — график (daily, owners, top)"
    )
    await m.answer(text, reply_markup=main_menu())


@dp.message(Command("stats"))
@dp.callback_query(F.data == "stats")
async def cb_stats(event):
    msg = event if isinstance(event, Message) else event.message
    if isinstance(event, CallbackQuery):
        await event.answer()
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        total = conn.execute("SELECT COUNT(*) FROM qa_log").fetchone()[0]
        with_fb = conn.execute("SELECT COUNT(DISTINCT qa_log_id) FROM qa_feedback").fetchone()[0]
        pos = conn.execute("SELECT COUNT(*) FROM qa_feedback WHERE is_correct = 1").fetchone()[0]
        neg = conn.execute("SELECT COUNT(*) FROM qa_feedback WHERE is_correct = 0").fetchone()[0]
        avg = conn.execute("SELECT AVG(latency_ms) FROM qa_log WHERE latency_ms IS NOT NULL").fetchone()[0]
        approved = conn.execute("SELECT COUNT(*) FROM qa_log WHERE sql_approved = 1").fetchone()[0]
        pool_n = conn.execute("SELECT COUNT(*) FROM few_shot_pool").fetchone()[0]
        warn = conn.execute("SELECT COUNT(*) FROM qa_log WHERE hallucination_warning = 1").fetchone()[0]
        top = conn.execute("SELECT question, COUNT(*) FROM qa_log GROUP BY LOWER(TRIM(question)) ORDER BY 2 DESC LIMIT 5").fetchall()
        conn.close()
        text = "📊 Статистика ИИ\n\n"
        text += "Всего вопросов: " + str(total) + "\n"
        text += "С оценкой: " + str(with_fb) + "\n"
        text += "✅ Верно: " + str(pos) + "\n"
        text += "❌ Неверно: " + str(neg) + "\n"
        text += "Одобрено для few-shot: " + str(approved) + "\n"
        text += "В пуле few-shot: " + str(pool_n) + "\n"
        text += "⚠️ Подозрение на галлюцинацию: " + str(warn) + "\n"
        if avg:
            text += "Среднее время: " + str(int(avg)) + " мс\n"
        text += "\nТоп-5 вопросов:\n"
        for q, n in top:
            text += "  • " + q[:40] + " — " + str(n) + " раз\n"
        await send_long(msg, text, reply_markup=main_menu())
    except Exception as e:
        await msg.answer("Ошибка статистики: " + str(e), reply_markup=main_menu())


@dp.message(Command("today"))
@dp.callback_query(F.data == "today")
async def cb_today(event):
    msg = event if isinstance(event, Message) else event.message
    if isinstance(event, CallbackQuery):
        await event.answer()
    await msg.answer("Читаю таблицу...")
    earnings = get_earnings()
    if not earnings:
        await msg.answer("Не удалось прочитать «Ввод данных».")
        return
    dates = [parse_date(e["date"]) for e in earnings]
    dates = [d for d in dates if d]
    if not dates:
        await msg.answer("Нет данных.", reply_markup=main_menu())
        return
    r = calc_day(earnings, max(dates).isoformat())
    if not r:
        await msg.answer("Нет данных.", reply_markup=main_menu())
        return
    text = "📅 Финансы за " + r["date"] + "\n\n"
    text += "🚗 Записей: " + str(r["count"]) + "\n"
    text += "💰 Всего: " + fmt(r["total"]) + " ₽\n\n"
    text += "🟢 АБ: " + fmt(r["ab_gross"]) + " − 21150 − 666 = " + fmt(r["ab_net"]) + " ₽\n"
    text += "🔵 Даниил: " + fmt(r["dan_gross"]) + " − 666 = " + fmt(r["dan_net"]) + " ₽\n"
    text += "🌸 Анна: " + fmt(r["anna_gross"]) + " + 1332 = " + fmt(r["anna_total"]) + " ₽"
    await msg.answer(text, reply_markup=main_menu())


@dp.message(Command("month"))
@dp.callback_query(F.data == "month")
async def cb_month(event):
    msg = event if isinstance(event, Message) else event.message
    if isinstance(event, CallbackQuery):
        await event.answer()
    await msg.answer("Читаю таблицу...")
    earnings = get_earnings()
    if not earnings:
        await msg.answer("Не удалось прочитать данные.")
        return
    today = date.today()
    first = today.replace(day=1)
    r = calc_period(earnings, first.isoformat(), today.isoformat())
    if not r:
        await msg.answer("Данных за месяц нет.", reply_markup=main_menu())
        return
    text = "📅 " + r["from"] + " — " + r["to"] + "\nДней: " + str(r["days"]) + "\n\n"
    text += "🚗 Всего: " + fmt(r["total"]) + " ₽\n\n"
    text += "🟢 АБ: " + fmt(r["ab_gross"]) + " → " + fmt(r["ab_net"]) + " ₽\n"
    text += "🔵 Даниил: " + fmt(r["dan_gross"]) + " → " + fmt(r["dan_net"]) + " ₽\n"
    text += "🌸 Анна: " + fmt(r["anna_total"]) + " ₽"
    await msg.answer(text, reply_markup=main_menu())


@dp.message(Command("cars"))
@dp.callback_query(F.data == "cars")
async def cb_cars(event):
    msg = event if isinstance(event, Message) else event.message
    if isinstance(event, CallbackQuery):
        await event.answer()
    await msg.answer("Читаю таблицу...")
    cars = get_cars()
    if not cars:
        await msg.answer("Не удалось прочитать «Автомобили».")
        return
    text = "🚗 Всего машин: " + str(len(cars)) + "\n\n"
    for gos, c in sorted(cars.items()):
        text += "• " + gos + " " + c.get("brand","") + " " + c.get("model","") + " - " + c.get("owner","?") + "\n"
    await send_long(msg, text, reply_markup=main_menu())


@dp.message(Command("owners"))
@dp.callback_query(F.data == "owners")
async def cb_owners(event):
    msg = event if isinstance(event, Message) else event.message
    if isinstance(event, CallbackQuery):
        await event.answer()
    await msg.answer("Читаю таблицу...")
    cars = get_cars()
    if not cars:
        await msg.answer("Не удалось прочитать.")
        return
    cnt = {}
    for c in cars.values():
        cnt[c["owner"]] = cnt.get(c["owner"], 0) + 1
    text = "👥 Машины по владельцам:\n\n"
    for o, n in sorted(cnt.items(), key=lambda x: -x[1]):
        text += "• " + o + ": " + str(n) + " машин\n"
    await msg.answer(text, reply_markup=main_menu())


@dp.message(Command("cash"))
@dp.callback_query(F.data == "cash")
async def cb_cash(event):
    msg = event if isinstance(event, Message) else event.message
    if isinstance(event, CallbackQuery):
        await event.answer()
    await msg.answer("Читаю кассу...")
    ops = get_cash()
    if not ops:
        await msg.answer("Не удалось прочитать «Касса».")
        return
    dates = [parse_date(x["date"]) for x in ops]
    dates = [d for d in dates if d]
    if not dates:
        await msg.answer("Нет данных.", reply_markup=main_menu())
        return
    last = max(dates).isoformat()
    inc = sum(x["sum"] for x in ops if x["type"] == "Приход" and x["date"] == last)
    exp = sum(x["sum"] for x in ops if x["type"] == "Расход" and x["date"] == last)
    diff = inc - exp
    text = "💰 Касса за " + last + "\n\nПриходы: " + fmt(inc) + " ₽\nРасходы: " + fmt(exp) + " ₽\nРазница: " + ("+" if diff >= 0 else "") + fmt(diff) + " ₽"
    await msg.answer(text, reply_markup=main_menu())


@dp.message(Command("balance"))
@dp.callback_query(F.data == "balance")
async def cb_balance(event):
    msg = event if isinstance(event, Message) else event.message
    if isinstance(event, CallbackQuery):
        await event.answer()
    b = get_balances()
    if not b:
        await msg.answer("Не удалось прочитать «Балансы».")
        return
    text = "🏦 Баланс на " + b["date"] + "\n\nКарта: " + fmt(b["card"]) + " ₽\nСчёт: " + fmt(b["account"]) + " ₽\nНаличные: " + fmt(b["cash"]) + " ₽\n─────────────────\nВСЕГО: " + fmt(b["total"]) + " ₽"
    await msg.answer(text, reply_markup=main_menu())


def _money(v):
    if v is None:
        return "—"
    n = float(v)
    if n % 1:
        s = f"{n:,.2f}".rstrip("0").rstrip(".")
    else:
        s = f"{n:,.0f}"
    return s.replace(",", " ") + " ₽"


def load_dashboard():
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        lines = ["📊 ДАШБОРД ТАКСОПАРКА"]
        latest = conn.execute(
            "SELECT date FROM earnings WHERE date IS NOT NULL AND TRIM(date) <> '' "
            "ORDER BY substr(date,7,4) DESC, substr(date,4,2) DESC, substr(date,1,2) DESC LIMIT 1"
        ).fetchone()
        if latest:
            d = latest["date"]
            lines.append(f"\n💰 За {d}:")
            for r in conn.execute(
                "SELECT COALESCE(NULLIF(TRIM(owner),''),'?') o, SUM(amount) t "
                "FROM earnings WHERE date=? GROUP BY o ORDER BY t DESC", (d,)
            ):
                lines.append(f"  • {r['o']}: {_money(r['t'])}")
            total = conn.execute("SELECT SUM(amount) FROM earnings WHERE date=?", (d,)).fetchone()[0]
            lines.append(f"  Итого: {_money(total)}")
        b = conn.execute(
            "SELECT date, card, account, cash, total FROM balances "
            "ORDER BY substr(date,7,4) DESC, substr(date,4,2) DESC, substr(date,1,2) DESC LIMIT 1"
        ).fetchone()
        if b:
            lines.append(f"\n🏦 Баланс на {b['date']}:")
            lines.append(f"  Карта: {_money(b['card'])}")
            lines.append(f"  Счёт: {_money(b['account'])}")
            lines.append(f"  Нал: {_money(b['cash'])}")
            lines.append(f"  ВСЕГО: {_money(b['total'])}")
        if latest:
            d = latest["date"]
            month = d[3:5]
            year = d[6:10]
            top = conn.execute(
                "SELECT gos, SUM(amount) t FROM earnings "
                "WHERE substr(date,4,2)=? AND substr(date,7,4)=? "
                "GROUP BY gos ORDER BY t DESC LIMIT 3",
                (month, year)
            ).fetchall()
            if top:
                lines.append(f"\n🚗 Топ-3 за {month}.{year}:")
                for r in top:
                    lines.append(f"  • {r['gos']}: {_money(r['t'])}")
        try:
            qa = conn.execute(
                "SELECT COUNT(*) total, SUM(CASE WHEN is_correct=1 THEN 1 ELSE 0 END) yes "
                "FROM qa_feedback WHERE created_at >= datetime('now','-7 days')"
            ).fetchone()
            qn = qa["total"] or 0
            qy = qa["yes"] or 0
            pct = round(100.0 * qy / qn, 1) if qn else 0
            lines.append(f"\n🤖 ИИ за 7 дней: {qn} оценок, ✅ {pct}%")
        except Exception:
            pass
    return "\n".join(lines)


@dp.message(Command("dashboard"))
async def cmd_dashboard(m: Message):
    if not is_owner(m.from_user.id):
        await m.answer("Нет доступа.")
        return
    try:
        text = await asyncio.to_thread(load_dashboard)
        await m.answer(text[:4000], reply_markup=main_menu())
    except Exception as e:
        await m.answer("Ошибка дашборда: " + str(e), reply_markup=main_menu())


@dp.callback_query(FeedbackCB.filter())
async def handle_feedback(callback: CallbackQuery, callback_data: FeedbackCB):
    try:
        found = await asyncio.to_thread(save_feedback, callback_data.qa_id, callback.from_user.id, callback_data.vote)
    except Exception:
        logging.exception("feedback")
        await callback.answer("Не удалось сохранить оценку.", show_alert=True)
        return
    if not found:
        await callback.answer("Ответ не найден.", show_alert=True)
        return
    await callback.answer("Спасибо! Оценка сохранена.")


@dp.message(Command("ai"))
@dp.message(F.text & ~F.text.startswith("/"))
async def on_text(m: Message):
    if not is_owner(m.from_user.id) or not m.text:
        return
    question = m.text.strip()
    await m.answer("🤔 Думаю... 5-15 секунд.")

    started = time.perf_counter()
    try:
        result = await asyncio.to_thread(ask_ai, question)
        if isinstance(result, tuple) and len(result) == 3:
            answer, sql, sql_rows = result
        elif isinstance(result, tuple) and len(result) == 2:
            answer, sql = result
            sql_rows = []
        else:
            answer, sql, sql_rows = str(result), None, []
    except Exception:
        logging.exception("ask_ai")
        answer, sql, sql_rows = "Не удалось обработать вопрос.", None, []

    latency_ms = int((time.perf_counter() - started) * 1000)
    q_type = classify_question(question)

    try:
        qa_id = await asyncio.to_thread(save_qa_log, m.from_user.id, question, q_type, sql, answer, latency_ms)
    except Exception:
        logging.exception("save_qa_log")
        await send_long(m, answer, reply_markup=main_menu())
        return

    if sql:
        try:
            warning, unsupported = check_numeric_grounding(answer, sql_rows)
            if warning:
                await asyncio.to_thread(save_hallucination_flag, qa_id, True)
                logging.warning("Possible hallucination qa_log_id=%s, unsupported=%s", qa_id, unsupported)
        except Exception:
            logging.exception("hallucination check")

    await send_long(m, answer, reply_markup=feedback_keyboard(qa_id))


def configure_scheduler_logging():
    from pathlib import Path
    log_path = (Path("logs") / "scheduler.log").resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("apscheduler")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = None
    for old in list(logger.handlers):
        same = isinstance(old, logging.FileHandler) and Path(old.baseFilename).resolve() == log_path
        if same and handler is None:
            handler = old
        else:
            logger.removeHandler(old)
            old.close()
    if handler is None:
        handler = logging.FileHandler(log_path, encoding="utf-8")
        logger.addHandler(handler)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))


async def main():
    init()

    try:
        await asyncio.to_thread(migrate_db)
        print("[init] DB migration OK")
    except Exception as e:
        print(f"[init] migration error: {e}")

    dp.include_router(commands.router)

    owner_id = int(os.getenv("BOT_OWNER_ID", "0") or "0")

    configure_scheduler_logging()
    sch = AsyncIOScheduler(timezone="Europe/Moscow")

    async def scheduled_rebuild():
        try:
            n = await rebuild_fewshot_pool_locked()
            print(f"[scheduler] few_shot_pool пересобран: {n} примеров")
        except Exception as e:
            print(f"[scheduler] rebuild error: {e}")

    async def weekly_job():
        if owner_id:
            try:
                await weekly_report_job(bot, owner_id)
            except Exception as e:
                print(f"[scheduler] weekly error: {e}")

    async def quality_job():
        if owner_id:
            try:
                await quality_check_job(bot, owner_id)
            except Exception as e:
                print(f"[scheduler] quality error: {e}")

    sch.add_job(scheduled_rebuild, CronTrigger(hour=3, minute=15),
                id="fewshot_rebuild", replace_existing=True,
                max_instances=1, coalesce=True)
    sch.add_job(weekly_job, CronTrigger(day_of_week="mon", hour=9, minute=0),
                id="weekly_report", replace_existing=True,
                max_instances=1, coalesce=True)
    sch.add_job(quality_job, CronTrigger(hour=4, minute=0),
                id="quality_check", replace_existing=True,
                max_instances=1, coalesce=True)

    try:
        if await asyncio.to_thread(needs_catchup):
            print("[scheduler] catch-up: пропущен rebuild — пересобираю")
            await scheduled_rebuild()
    except Exception as e:
        print(f"[scheduler] catchup error: {e}")

    sch.start()

    print("Bot started")
    try:
        await dp.start_polling(bot)
    finally:
        sch.shutdown(wait=False)


if __name__ == "__main__":
    asyncio.run(main())