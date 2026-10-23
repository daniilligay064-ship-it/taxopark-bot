# -*- coding: utf-8 -*-
import sqlite3
import time
from contextlib import closing

from paths import DB

LIMIT_DEFAULT = 30       # 30 вопросов в час
WINDOW_DEFAULT = 3600    # окно в секундах


def reserve_question(user_id, limit=LIMIT_DEFAULT, window=WINDOW_DEFAULT):
    """Атомарно резервирует место. Возвращает (allowed, retry_after_sec)."""
    now = int(time.time())
    cutoff = now - window
    with closing(sqlite3.connect(DB, timeout=10)) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS request_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_request_events_user_time
            ON request_events(user_id, created_at)
        """)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM request_events WHERE created_at <= ?", (cutoff,))
        count, earliest = conn.execute("""
            SELECT COUNT(*), MIN(created_at)
            FROM request_events
            WHERE user_id = ? AND created_at > ?
        """, (user_id, cutoff)).fetchone()
        if count >= limit:
            conn.commit()
            retry_after = max(1, window - (now - earliest)) if earliest else window
            return False, retry_after
        conn.execute(
            "INSERT INTO request_events(user_id, created_at) VALUES (?, ?)",
            (user_id, now)
        )
        conn.commit()
        return True, 0