# -*- coding: utf-8 -*-
import asyncio
import sqlite3
import time
from html import escape
from math import ceil

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.enums import ParseMode

from config import OWNERS
from scheduler import rebuild_fewshot_pool

from paths import DB_PATH
router = Router()
rebuild_lock = asyncio.Lock()

POOL_EXPLAIN_SQL = """
SELECT
    p.slot, p.qa_log_id, p.question, p.sql_text,
    p.question_type, p.semantic_key, q.sql_approved,
    COUNT(DISTINCT CASE WHEN f.is_correct=1 THEN f.voter_id END) AS positive_users,
    SUM(CASE WHEN f.is_correct=1 THEN 1 ELSE 0 END) AS positive_votes,
    SUM(CASE WHEN f.is_correct=0 THEN 1 ELSE 0 END) AS negative_votes
FROM few_shot_pool AS p
JOIN qa_log AS q ON q.id = p.qa_log_id
LEFT JOIN qa_feedback AS f ON f.qa_log_id = p.qa_log_id
GROUP BY p.slot, p.qa_log_id, p.question, p.sql_text,
         p.question_type, p.semantic_key, q.sql_approved
ORDER BY p.slot
"""


def _reserve_cooldown():
    now = time.time()
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT value FROM app_state WHERE key='manual_rebuild_epoch'").fetchone()
        if row:
            remaining = 300 - (now - float(row[0]))
            if remaining > 0:
                conn.rollback()
                return False, ceil(remaining)
        conn.execute(
            "INSERT INTO app_state(key,value) VALUES('manual_rebuild_epoch',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(now),)
        )
        conn.commit()
    return True, 0


def _load_pool():
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(POOL_EXPLAIN_SQL).fetchall()]


async def run_rebuild_locked():
    async with rebuild_lock:
        return await asyncio.to_thread(rebuild_fewshot_pool)


@router.message(Command("rebuild"))
async def cmd_rebuild(m: Message):
    if m.from_user.id not in OWNERS:
        await m.answer("Нет доступа.")
        return
    allowed, remaining = await asyncio.to_thread(_reserve_cooldown)
    if not allowed:
        await m.answer(f"Повторно через {remaining} сек.")
        return
    try:
        n = await run_rebuild_locked()
    except Exception as e:
        await m.answer(f"Ошибка пересборки: {e}")
        return
    await m.answer(f"Пул пересобран: {n} примеров.")


@router.message(Command("explain"))
async def cmd_explain(m: Message):
    if m.from_user.id not in OWNERS:
        await m.answer("Нет доступа.")
        return
    rows = await asyncio.to_thread(_load_pool)
    if not rows:
        await m.answer("Few-shot пул пуст.")
        return
    parts = ["<b>Few-shot пул</b>"]
    for r in rows:
        q = escape(str(r["question"])[:180])
        sql = escape(str(r["sql_text"] or "")[:300])
        t = escape(str(r["question_type"] or "other"))
        parts.append(
            f"\n<b>{r['slot']}. {q}</b>\n"
            f"Тип: {t}\n"
            f"✅ {r['positive_votes']} от {r['positive_users']} чел., ❌ {r['negative_votes']}\n"
            f"SQL: <code>{sql}</code>"
        )
    await m.answer("\n".join(parts)[:3900], parse_mode=ParseMode.HTML)