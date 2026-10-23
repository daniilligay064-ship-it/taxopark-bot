# -*- coding: utf-8 -*-
import csv
import os
import re
import sqlite3
import tempfile
from datetime import datetime
from decimal import Decimal, InvalidOperation

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

matplotlib.rcParams["font.family"] = "DejaVu Sans"
matplotlib.rcParams["axes.unicode_minus"] = False

from paths import DB

EXPORT_COLUMNS = {
    "earnings": ["id", "date", "gos", "driver", "amount", "owner"],
    "cash": ["id", "date", "type", "category", "amount", "who"],
    "balances": ["id", "date", "card", "account", "cash", "total"],
    "cars": ["plate", "owner", "brand", "model"],
}

DATE_SQL = "(substr(trim(date),7,4)||'-'||substr(trim(date),4,2)||'-'||substr(trim(date),1,2))"


def to_iso_date(s):
    return datetime.strptime(s, "%d.%m.%Y").date().isoformat()


def excel_safe(v):
    if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + v
    return v


def create_export(table, start=None, end=None):
    if table not in EXPORT_COLUMNS:
        raise ValueError("Неизвестная таблица")
    cols = EXPORT_COLUMNS[table]
    sql = "SELECT " + ", ".join(cols) + " FROM " + table
    params = []
    if table != "cars" and start and end:
        sql += " WHERE " + DATE_SQL + " BETWEEN ? AND ?"
        params = [to_iso_date(start), to_iso_date(end)]
    sql += " ORDER BY 1"

    fd, path = tempfile.mkstemp(suffix=".csv", prefix="export_")
    os.close(fd)
    with sqlite3.connect(DB, timeout=15) as conn, open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(cols)
        for row in conn.execute(sql, params):
            w.writerow([excel_safe(v) for v in row])
    return path


def create_chart(kind, start, end):
    start_iso = to_iso_date(start)
    end_iso = to_iso_date(end)
    if start_iso > end_iso:
        raise ValueError("Начало позже конца")

    if kind == "daily":
        sql = f"SELECT trim(date), SUM(amount) FROM earnings WHERE {DATE_SQL} BETWEEN ? AND ? GROUP BY trim(date) ORDER BY {DATE_SQL}"
        title, xlabel, ylabel = "Выручка по дням", "Дата", "Выручка, ₽"
    elif kind == "owners":
        sql = f"SELECT COALESCE(NULLIF(trim(owner),''),'?'), SUM(amount) FROM earnings WHERE {DATE_SQL} BETWEEN ? AND ? GROUP BY 1 ORDER BY SUM(amount) DESC"
        title, xlabel, ylabel = "Выручка по владельцам", "Владелец", "Выручка, ₽"
    elif kind == "top":
        sql = f"SELECT trim(gos), SUM(amount) FROM earnings WHERE {DATE_SQL} BETWEEN ? AND ? AND gos IS NOT NULL AND trim(gos)<>'' GROUP BY 1 ORDER BY SUM(amount) DESC LIMIT 10"
        title, xlabel, ylabel = "Топ-10 машин", "Госномер", "Выручка, ₽"
    else:
        raise ValueError("Тип: daily, owners, top")

    with sqlite3.connect(DB, timeout=15) as conn:
        rows = conn.execute(sql, (start_iso, end_iso)).fetchall()
    if not rows:
        return None

    labels = [r[0] for r in rows]
    values = [float(r[1]) for r in rows]

    fd, path = tempfile.mkstemp(suffix=".png", prefix="chart_")
    os.close(fd)

    fig, ax = plt.subplots(figsize=(10, 6))
    if kind == "daily":
        ax.plot(labels, values, marker="o", color="#1a73e8", linewidth=2)
        plt.xticks(rotation=45, ha="right")
    else:
        ax.bar(labels, values, color="#34a853")
        if kind == "top":
            plt.xticks(rotation=45, ha="right")

    ax.set_title(title)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{int(x):,}".replace(",", " ")))
    plt.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path


# ============ ПРОВЕРКА ЧИСЕЛ (галлюцинации) ============

DATE_RE = re.compile(r"\b\d{1,2}\.\d{1,2}\.\d{4}\b")
YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
NUM_RE = re.compile(r"(?<!\w)(?:\d{1,3}(?:[ \u00a0\u202f.,]\d{3})+|\d+)(?:[.,]\d+)?(?!\w)")


def normalized_number(token):
    s = re.sub(r"[\s\u00a0\u202f]", "", token)
    if "." in s and "," in s:
        dec = "." if s.rfind(".") > s.rfind(",") else ","
        th = "," if dec == "." else "."
        s = s.replace(th, "").replace(dec, ".")
    elif re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", s):
        s = s.replace(".", "").replace(",", "")
    else:
        s = s.replace(",", ".")
    try:
        return Decimal(s).normalize()
    except InvalidOperation:
        return None


def check_numeric_grounding(answer, sql_rows):
    text = YEAR_RE.sub(" ", DATE_RE.sub(" ", answer or ""))
    answer_nums = set()
    for t in NUM_RE.findall(text):
        n = normalized_number(t)
        if n is not None and abs(n) >= 1000:
            answer_nums.add(n)

    sql_nums = set()
    for row in sql_rows or []:
        vals = row.values() if isinstance(row, dict) else row
        for v in vals:
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                try:
                    sql_nums.add(Decimal(str(v)).normalize())
                except InvalidOperation:
                    pass

    unsupported = answer_nums - sql_nums
    return bool(unsupported), sorted(str(x) for x in unsupported)