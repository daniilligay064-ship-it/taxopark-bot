# -*- coding: utf-8 -*-
import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from paths import DB
TZ = ZoneInfo("Europe/Berlin")
REBUILD_LOCK = asyncio.Lock()

POOL_SQL = """
WITH stats AS (
    SELECT
        q.id, q.question, q.corrected_sql,
        COALESCE(NULLIF(TRIM(q.question_type), ''), 'other') AS question_type,
        TRIM(q.semantic_key) AS semantic_key,
        COALESCE(NULLIF(LOWER(TRIM(q.owner_key)), ''), '__unknown__') AS owner_key_norm,
        q.created_at,
        COUNT(DISTINCT CASE WHEN f.is_correct = 1 THEN f.voter_id END) AS positive_users,
        SUM(CASE WHEN f.is_correct = 1 THEN 1 ELSE 0 END) AS positive_votes,
        SUM(CASE WHEN f.is_correct = 0 THEN 1 ELSE 0 END) AS negative_votes
    FROM qa_log AS q
    LEFT JOIN qa_feedback AS f ON f.qa_log_id = q.id
    WHERE q.sql_approved = 1
      AND q.corrected_sql IS NOT NULL
      AND TRIM(q.corrected_sql) <> ''
      AND q.semantic_key IS NOT NULL
      AND TRIM(q.semantic_key) <> ''
    GROUP BY q.id, q.question, q.corrected_sql,
             q.question_type, q.semantic_key, q.owner_key, q.created_at
),
eligible AS (
    SELECT * FROM stats
    WHERE positive_users >= 1
      AND positive_votes >= 1
      AND negative_votes = 0
),
dedup_owner AS (
    SELECT *,
        ROW_NUMBER() OVER (
            PARTITION BY semantic_key, owner_key_norm
            ORDER BY positive_users DESC, positive_votes DESC, created_at DESC, id DESC
        ) AS owner_rank
    FROM eligible
),
owner_unique AS (
    SELECT * FROM dedup_owner WHERE owner_rank = 1
),
ranked_semantic AS (
    SELECT *,
        ROW_NUMBER() OVER (
            PARTITION BY semantic_key
            ORDER BY positive_users DESC, positive_votes DESC, created_at DESC, id DESC
        ) AS semantic_rank
    FROM owner_unique
),
limited_semantic AS (
    SELECT * FROM ranked_semantic WHERE semantic_rank <= 2
),
ranked_types AS (
    SELECT *,
        ROW_NUMBER() OVER (
            PARTITION BY question_type
            ORDER BY positive_users DESC, positive_votes DESC, created_at DESC, id DESC
        ) AS type_rank
    FROM limited_semantic
)
SELECT id, question, corrected_sql, question_type, semantic_key
FROM ranked_types
ORDER BY CASE WHEN type_rank = 1 THEN 0 ELSE 1 END,
         type_rank, positive_users DESC, positive_votes DESC, created_at DESC, id DESC
LIMIT 5
"""


def rebuild_fewshot_pool():
    conn = sqlite3.connect(DB, timeout=30)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        rows = conn.execute(POOL_SQL).fetchall()
        conn.execute("DELETE FROM few_shot_pool")
        if rows:
            conn.executemany(
                "INSERT INTO few_shot_pool (slot, qa_log_id, question, sql_text, question_type, semantic_key) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(slot, qa_id, question, sql, qtype, semkey)
                 for slot, (qa_id, question, sql, qtype, semkey) in enumerate(rows, start=1)]
            )
        conn.execute(
            "INSERT INTO app_state(key,value) VALUES('last_fewshot_rebuild',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (datetime.now(TZ).isoformat(timespec="seconds"),)
        )
        conn.commit()
        return len(rows)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def rebuild_fewshot_pool_locked():
    async with REBUILD_LOCK:
        return await asyncio.to_thread(rebuild_fewshot_pool)


def needs_catchup():
    now = datetime.now(TZ)
    due = now.replace(hour=3, minute=15, second=0, microsecond=0)
    if now < due:
        due -= timedelta(days=1)
    with sqlite3.connect(DB, timeout=10) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        row = conn.execute("SELECT value FROM app_state WHERE key='last_fewshot_rebuild'").fetchone()
    if not row:
        return True
    try:
        last = datetime.fromisoformat(row[0])
    except (TypeError, ValueError):
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=TZ)
    else:
        last = last.astimezone(TZ)
    return last < due


# ============ WEEKLY REPORT ============

def get_weekly_stats():
    conn = sqlite3.connect(DB, timeout=10)
    try:
        cur = conn.execute("""
            SELECT
                SUM(CASE WHEN created_at >= datetime('now','-7 days') THEN 1 ELSE 0 END),
                SUM(CASE WHEN created_at >= datetime('now','-7 days') AND is_correct=1 THEN 1 ELSE 0 END),
                SUM(CASE WHEN created_at < datetime('now','-7 days') AND created_at >= datetime('now','-14 days') THEN 1 ELSE 0 END),
                SUM(CASE WHEN created_at < datetime('now','-7 days') AND created_at >= datetime('now','-14 days') AND is_correct=1 THEN 1 ELSE 0 END)
            FROM qa_feedback
            WHERE created_at >= datetime('now','-14 days')
        """)
        row = cur.fetchone() or (0, 0, 0, 0)
        rn, ry, pn, py = (x or 0 for x in row)
        rp = round(100.0 * ry / rn, 1) if rn else 0.0
        pp = round(100.0 * py / pn, 1) if pn else 0.0
        return rn, rp, pn, pp
    finally:
        conn.close()


def save_weekly_report(period_start, period_end, metrics_text, report_text):
    conn = sqlite3.connect(DB, timeout=10)
    try:
        conn.execute("""
            INSERT INTO weekly_report (period_start, period_end, metrics_json, report_text)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(period_start, period_end) DO UPDATE SET
                metrics_json = excluded.metrics_json,
                report_text = excluded.report_text
        """, (period_start, period_end, metrics_text, report_text))
        conn.commit()
    finally:
        conn.close()


def report_was_sent(period_start, period_end):
    conn = sqlite3.connect(DB, timeout=10)
    try:
        row = conn.execute(
            "SELECT 1 FROM weekly_report WHERE period_start=? AND period_end=? AND sent_at IS NOT NULL LIMIT 1",
            (period_start, period_end)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def mark_report_sent(period_start, period_end):
    conn = sqlite3.connect(DB, timeout=10)
    try:
        conn.execute(
            "UPDATE weekly_report SET sent_at=CURRENT_TIMESTAMP WHERE period_start=? AND period_end=?",
            (period_start, period_end)
        )
        conn.commit()
    finally:
        conn.close()


async def weekly_report_job(bot, owner_id):
    try:
        rn, rp, pn, pp = await asyncio.to_thread(get_weekly_stats)
    except Exception:
        logging.exception("weekly stats failed")
        return
    today = datetime.now().date()
    period_end = today.isoformat()
    period_start = (today - timedelta(days=7)).isoformat()
    metrics_text = f"recent_n={rn}; recent_pct={rp}; prev_n={pn}; prev_pct={pp}"
    degrades = (rn >= 20 and pn >= 20 and (pp - rp) >= 15.0)
    if not degrades:
        await asyncio.to_thread(save_weekly_report, period_start, period_end, metrics_text, "OK")
        return
    if await asyncio.to_thread(report_was_sent, period_start, period_end):
        return
    text = (
        "⚠️ Отчёт ИИ за неделю\n\n"
        f"Период: {period_start} — {period_end}\n"
        f"За неделю: {rn} голосов, ✅ {rp}%\n"
        f"За прошлую: {pn} голосов, ✅ {pp}%\n"
        f"Падение: {round(pp - rp, 1)} п.п."
    )
    try:
        await bot.send_message(owner_id, text)
        await asyncio.to_thread(save_weekly_report, period_start, period_end, metrics_text, text)
        await asyncio.to_thread(mark_report_sent, period_start, period_end)
    except Exception:
        logging.exception("send weekly report failed")


# ============ QUALITY CHECK ============

CHECKS = [
    ("earnings без владельца", "earnings",
     "owner IS NULL OR TRIM(owner) = '' OR TRIM(owner) = '?'", "id"),
    ("cars без владельца", "cars",
     "owner IS NULL OR TRIM(owner) = '' OR TRIM(owner) = '?'", "plate"),
    ("cash без даты", "cash",
     "date IS NULL OR TRIM(date) = ''", "id"),
    ("balances без даты", "balances",
     "date IS NULL OR TRIM(date) = ''", "id"),
]


def collect_quality_issues():
    issues = []
    with sqlite3.connect(DB, timeout=10) as conn:
        for label, table, condition, sample_col in CHECKS:
            try:
                count = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {condition}"
                ).fetchone()[0]
                if count:
                    sample = conn.execute(
                        f"SELECT {sample_col} FROM {table} WHERE {condition} LIMIT 10"
                    ).fetchall()
                    examples = ", ".join(str(row[0]) for row in sample)
                    issues.append(f"⚠️ {label}: {count}. Примеры: {examples}")
            except Exception as e:
                issues.append(f"⚠️ {label}: ошибка проверки ({e})")
    return issues


async def quality_check_job(bot, owner_id):
    try:
        issues = await asyncio.to_thread(collect_quality_issues)
    except Exception:
        logging.exception("quality check failed")
        return
    if not issues:
        return
    text = "🔎 Проверка качества базы\n\n" + "\n".join(issues)
    try:
        await bot.send_message(owner_id, text[:4000])
    except Exception:
        logging.exception("send quality report failed")


# ============ DAILY BACKUP ============

async def backup_job(bot, owner_id):
    """Ежедневный бэкап SQLite в 02:00."""
    try:
        from backup import backup_database
        path = await asyncio.to_thread(backup_database)
        logging.info("Backup created: %s", path)
    except Exception:
        logging.exception("Backup failed")
        if owner_id:
            try:
                await bot.send_message(owner_id, "⚠️ Не удалось создать резервную копию SQLite!")
            except Exception:
                logging.exception("Не удалось отправить уведомление о бэкапе")