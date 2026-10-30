# -*- coding: utf-8 -*-
import csv
import io
import os
import re
import sqlite3
import urllib.request

PUB = "2PACX-1vRasawikvcH-lhgkL2bEOx5U2uHsORPXhMsNaYcxpqFuln8PKkUPcATXUfKNMhsJAjVl4FYS2EaUVjq"

DB = r"C:\taxipark_bot\data\taxopark.db"
TEST_DATE = "31.12.2026"

CYR_TO_LAT = str.maketrans({
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H",
    "О": "O", "Р": "P", "С": "C", "Т": "T", "У": "Y", "Х": "X",
})


def num(v):
    if v is None or v == "":
        return 0.0
    s = str(v).replace("\xa0", "").replace(" ", "").replace("₽", "").replace("р.", "")
    s = re.sub(r"[^\d.\-]", "", s.replace(",", "."))
    try:
        return float(s)
    except:
        return 0.0


def normalize_plate(s):
    """Приводит госномер к латинице, убирает пробелы и дефисы."""
    if not s:
        return ""
    s = str(s).strip().upper()
    s = "".join(ch for ch in s if not ch.isspace() and ch != "-")
    return s.translate(CYR_TO_LAT)


def fetch(name):
    from sheets_helper import read_sheet
    return read_sheet(name)


def sync():
    os.makedirs(r"C:\taxipark_bot\data", exist_ok=True)
    conn = sqlite3.connect(DB, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    c = conn.cursor()

    # === cars ===
    c.execute("DROP TABLE IF EXISTS cars")
    c.execute("CREATE TABLE cars (plate TEXT PRIMARY KEY, plate_norm TEXT, owner TEXT, brand TEXT, model TEXT)")
    for r in fetch("Автомобили"):
        plate_raw = (r.get("Госномер") or "").strip().upper()
        plate_n = normalize_plate(plate_raw)
        if not plate_n:
            continue
        owner = (r.get("Владелец") or "").strip() or "Александр Борисович"
        c.execute("INSERT OR REPLACE INTO cars VALUES (?,?,?,?,?)",
                  (plate_n, plate_n, owner,
                   (r.get("Марка") or "").strip(), (r.get("Модель") or "").strip()))
    c.execute("CREATE INDEX IF NOT EXISTS idx_cars_norm ON cars(plate_norm)")

    # === earnings ===
    c.execute("DROP TABLE IF EXISTS earnings")
    c.execute("CREATE TABLE earnings (id TEXT PRIMARY KEY, date TEXT, gos TEXT, gos_norm TEXT, driver TEXT, amount REAL, owner TEXT)")

    rows = fetch("Ввод данных")

    # Тестовая запись
    test_rows = [r for r in rows if (r.get("Дата") or "").strip() == TEST_DATE]
    print("Тестовых записей " + TEST_DATE + ": " + str(len(test_rows)))
    if len(test_rows) > 0:
        print("  Исключаю все " + str(len(test_rows)) + " записей с датой " + TEST_DATE)
        rows = [r for r in rows if (r.get("Дата") or "").strip() != TEST_DATE]

    i = 0
    for r in rows:
        amt = num(r.get("Заработок"))
        if amt <= 0:
            continue
        gos_raw = (r.get("Госномер") or "").strip().upper()
        gos_n = normalize_plate(gos_raw)
        if not gos_n:
            continue
        sid = (r.get("ID записи") or "").strip() or ("OP-AUTO-" + str(i))
        i = i + 1
        owner_raw = (r.get("Владелец") or "").strip()
        c.execute("INSERT OR REPLACE INTO earnings VALUES (?,?,?,?,?,?,?)",
                  (sid, (r.get("Дата") or "").strip(), gos_raw, gos_n,
                   (r.get("Водитель") or "").strip(), amt, owner_raw))
    c.execute("CREATE INDEX IF NOT EXISTS idx_earnings_norm ON earnings(gos_norm)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_earnings_date ON earnings(date)")

    # === FIX OWNER через нормализованный JOIN ===
    c.execute("""
        UPDATE earnings
        SET owner = COALESCE(
            (SELECT NULLIF(TRIM(c.owner), '')
             FROM cars AS c
             WHERE c.plate_norm = earnings.gos_norm
             LIMIT 1),
            '?')
        WHERE owner IS NULL OR TRIM(owner) = ''
    """)
    c.execute("UPDATE earnings SET owner = '?' WHERE owner IS NULL OR TRIM(owner) = ''")

    # === cash ===
    c.execute("DROP TABLE IF EXISTS cash")
    c.execute("CREATE TABLE cash (id TEXT PRIMARY KEY, date TEXT, type TEXT, category TEXT, amount REAL, who TEXT)")
    i = 0
    for r in fetch("Касса"):
        amt = num(r.get("Сумма"))
        if amt <= 0:
            continue
        sid = (r.get("ID операции") or "").strip() or ("CASH-AUTO-" + str(i))
        i = i + 1
        c.execute("INSERT OR REPLACE INTO cash VALUES (?,?,?,?,?,?)",
                  (sid, (r.get("Дата") or "").strip(),
                   (r.get("Тип операции") or "").strip(),
                   (r.get("Категория") or "").strip(),
                   amt, (r.get("Получил из кассы") or "").strip()))

    # === balances ===
    c.execute("DROP TABLE IF EXISTS balances")
    c.execute("CREATE TABLE balances (id TEXT PRIMARY KEY, date TEXT, card REAL, account REAL, cash REAL, total REAL)")
    i = 0
    for r in fetch("Балансы"):
        sid = (r.get("Дата") or "").strip() or ("BAL-" + str(i))
        i = i + 1
        c.execute("INSERT OR REPLACE INTO balances VALUES (?,?,?,?,?,?)",
                  (sid, (r.get("Дата") or "").strip(),
                   num(r.get("Карта (₽)")), num(r.get("Счёт (₽)")),
                   num(r.get("Нал (₽)")), num(r.get("Всего (₽)"))))

    conn.commit()

    # Отчёт
    print()
    print("ИТОГО:")
    for t in ["cars", "earnings", "cash", "balances"]:
        c.execute("SELECT COUNT(*) FROM " + t)
        print("  " + t + ": " + str(c.fetchone()[0]))

    print()
    print("Владельцы в earnings:")
    c.execute("SELECT owner, COUNT(*), SUM(amount) FROM earnings GROUP BY owner ORDER BY 2 DESC")
    for row in c.fetchall():
        print("  " + str(row[0]) + ": " + str(row[1]) + " записей, " + str(int(row[2])) + " ₽")

    print()
    c.execute("SELECT COUNT(*) FROM earnings WHERE owner = '?'")
    print("Не распознано ('?'): " + str(c.fetchone()[0]))

    conn.close()


if __name__ == "__main__":
    print("Синхронизация...")
    sync()
    print("ГОТОВО")
