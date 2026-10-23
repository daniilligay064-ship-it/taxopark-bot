import sqlite3
from datetime import datetime

DB = "data/taxipark.db"

def conn():
    return sqlite3.connect(DB)

def init():
    c = conn()
    cur = c.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS cars(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        num TEXT UNIQUE, brand TEXT, model TEXT, owner TEXT,
        status TEXT, date_add TEXT, date_off TEXT, comment TEXT)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS sheet_cache(
        sheet TEXT PRIMARY KEY, data TEXT, updated TEXT)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS daily(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT, num TEXT, driver TEXT, gross REAL, cash REAL, comment TEXT)""")
    c.commit()
    c.close()

def save_cache(sheet, data_json):
    c = conn()
    c.execute("INSERT OR REPLACE INTO sheet_cache(sheet, data, updated) VALUES (?,?,?)",
              (sheet, data_json, datetime.now().isoformat()))
    c.commit()
    c.close()

def get_cache(sheet):
    c = conn()
    cur = c.execute("SELECT data FROM sheet_cache WHERE sheet=?", (sheet,))
    row = cur.fetchone()
    c.close()
    return row[0] if row else None
