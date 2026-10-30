# -*- coding: utf-8 -*-
import os
import re
import sqlite3

from dotenv import load_dotenv
load_dotenv()

from paths import DB
OR = os.getenv("OPENROUTER_KEY", "")
GQ = os.getenv("GROQ_KEY", "")

WR = {
    sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE,
    sqlite3.SQLITE_DELETE, sqlite3.SQLITE_DROP_TABLE,
    sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_PRAGMA,
    sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_INDEX,
    sqlite3.SQLITE_DROP_INDEX, sqlite3.SQLITE_DROP_VIEW,
    sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH,
}

SYSTEM_PROMPT = """You are an SQL generator EXCLUSIVELY for SQLite 3.

Schema:
cars(plate, owner, brand, model)
earnings(id, date, gos, driver, amount, owner)
cash(id, date, type, category, amount, who)
balances(id, date, card, account, cash, total)

Rules:
1. Generate ONLY one SELECT or WITH ... SELECT query.
2. NEVER use INSERT, UPDATE, DELETE, DROP.
3. NEVER use PostgreSQL/MySQL syntax: EXTRACT, DATE_FORMAT, TO_DATE, ILIKE, MONTH(), YEAR().
4. The date column is TEXT in format dd.MM.yyyy (example: 05.10.2026).
5. Filter by month and year using:
   substr(date, 4, 2) = '10' AND substr(date, 7, 4) = '2026'
   OR simply: date LIKE '%.10.2026'
6. Do NOT use strftime on dd.MM.yyyy dates.
7. Use SQL aggregates: SUM, AVG, COUNT.
8. Use only tables and columns from the schema.
9. Return ONLY SQL. No markdown, no comments, no explanation.
10. If impossible, return exactly: NO_SQL

JOIN: cars.plate = earnings.gos

ADDITIONAL RULES:
11. "Сколько заработал X" - only SUM(amount). NO deductions.
12. "Чистый доход X" - SUM(amount) - 666*COUNT(DISTINCT date) for Anna salary. For AB also - 21150*COUNT(DISTINCT date).
13. NEVER invent numbers.
14. If result empty, say no data.
15. For top-N: ORDER BY DESC LIMIT N.
"""

OWNERS = {"александр": "Александр Борисович", "анна": "Анна", "даниил": "Даниил", "данил": "Даниил"}


def schema():
    cn = sqlite3.connect(DB)
    c = cn.cursor()
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    s = ""
    for (t,) in c.fetchall():
        c.execute("PRAGMA table_info(" + t + ")")
        cols = [r[1] for r in c.fetchall()]
        c.execute("SELECT COUNT(*) FROM " + t)
        n = c.fetchone()[0]
        s += t + " (" + str(n) + "): " + ", ".join(cols) + "\n"
    cn.close()
    return s


def find_owner(q):
    ql = q.lower()
    for k, v in OWNERS.items():
        if k in ql:
            return v
    return None


def extract_sql(response):
    if not response:
        return None
    text = response.strip()
    text = re.sub(r"^```(?:sql)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()
    if not re.match(r"^(SELECT|WITH)\b", text, flags=re.I):
        return None
    if ";" in text.rstrip(";"):
        return None
    up = text.upper()
    for bad in ["EXTRACT", "DATE_FORMAT", "DATEDIFF", "NO_SQL", "SAFETY"]:
        if bad in up:
            return None
    return text.rstrip(";")


def read_sql(sql):
    q = sql.strip()
    if not re.match(r"^(SELECT|WITH)\b", q, re.I):
        return None, "Only SELECT allowed"
    if ";" in q.rstrip(";"):
        return None, "Multiple statements not allowed"
   from paths import DB_PATH
cn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=3)
    try:
        cn.execute("PRAGMA query_only=ON")
        cn.set_authorizer(lambda a, b, c, d, e: sqlite3.SQLITE_DENY if a in WR else sqlite3.SQLITE_OK)
        cur = cn.execute(q)
        cols = [d[0] for d in cur.description or []]
        return {"columns": cols, "rows": cur.fetchmany(201)[:200]}, None
    except Exception as e:
        return None, str(e)
    finally:
        cn.close()


def _llm(msgs, kind):
    if kind == "groq" and GQ:
        try:
            from groq import Groq
            cl = Groq(api_key=GQ)
            for m in ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"]:
                try:
                    r = cl.chat.completions.create(model=m, messages=msgs, timeout=60)
                    return r.choices[0].message.content
                except:
                    pass
        except:
            pass
    if kind == "openrouter" and OR:
        try:
            from openai import OpenAI
            cl = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=OR)
            for m in ["openrouter/free", "qwen/qwen3.8-27b:free", "meta-llama/llama-3.3-70b-instruct:free"]:
                try:
                    r = cl.chat.completions.create(model=m, messages=msgs, timeout=90)
                    return r.choices[0].message.content
                except:
                    pass
        except:
            pass
    return None
def load_fewshot():
    try:
        with sqlite3.connect(DB, timeout=5) as conn:
            return conn.execute(
                "SELECT question, sql_text FROM few_shot_pool ORDER BY slot LIMIT 5"
            ).fetchall()
    except Exception:
        return []


def ask_ai(q):
    sc = schema()
    owner = find_owner(q)
    hint = "Owner mentioned: " + owner + "\n" if owner else ""
    user_msg = "Schema:\n" + sc + "\nJOIN: cars.plate = earnings.gos\n" + hint + "Question: " + q

    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    # Few-shot примеры из пула: user/assistant парами
    for ex_q, ex_sql in load_fewshot():
        msgs.append({"role": "user", "content": ex_q})
        msgs.append({"role": "assistant", "content": ex_sql})
    msgs.append({"role": "user", "content": user_msg})

    sql = None
    for provider in ["groq", "openrouter"]:
        raw = _llm(msgs, provider)
        sql = extract_sql(raw)
        if sql:
            break

    if not sql:
        return "Не удалось сформировать корректный SQL. Попробуйте переформулировать.", None

    res, err = read_sql(sql)
    if err:
        return "Ошибка SQL: " + err + "\n\nЗапрос: " + sql, sql
    if not res or not res["rows"]:
        return "Данных за указанный период не найдено.", sql

    p2 = (
        "Ты аналитик таксопарка. Отвечай на русском, кратко и точно.\n"
        "Вопрос: " + q + "\n"
        "SQL: " + sql + "\n"
        "Результат SQL: " + str(res)
    )
    ans = _llm([{"role": "user", "content": p2}], "groq")
    if not ans:
        ans = _llm([{"role": "user", "content": p2}], "openrouter")
    return ans or str(res), sql
