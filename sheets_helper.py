# -*- coding: utf-8 -*-
import csv, io, re, urllib.request

PUB_ID = "2PACX-1vRasawikvcH-lhgkL2bEOx5U2uHsORPXhMsNaYcxpqFuln8PKkUPcATXUfKNMhsJAjVl4FYS2EaUVjq"
GIDS = {
    "Ввод данных": "517194363",
    "Касса": "1317235815",
    "Балансы": "628441157",
    "Автомобили": "165056063",
}

def _num(v):
    if v is None or v == "": return 0.0
    s = str(v).replace("\xa0", "").replace(" ", "").replace("₽", "").replace("р.", "")
    s = re.sub(r"[^\d.\-]", "", s.replace(",", "."))
    try: return float(s)
    except: return 0.0

def read_sheet(name):
    gid = GIDS.get(name)
    if not gid: return []
    url = f"https://docs.google.com/spreadsheets/d/e/{PUB_ID}/pub?gid={gid}&single=true&output=csv"
    try:
        data = urllib.request.urlopen(url, timeout=30).read().decode("utf-8")
    except Exception as e:
        print(f"err {name}: {e}"); return []
    return list(csv.DictReader(io.StringIO(data)))

def get_cars():
    cars = {}
    for r in read_sheet("Автомобили"):
        gos = (r.get("Госномер") or "").strip().upper()
        if not gos: continue
        cars[gos] = {"gos": gos, "owner": (r.get("Владелец") or "").strip() or "Александр Борисович"}
    return cars

def get_earnings():
    cars = get_cars(); res = []
    for r in read_sheet("Ввод данных"):
        amt = _num(r.get("Заработок") or 0)
        if amt <= 0: continue
        gos = (r.get("Госномер") or "").strip().upper()
        owner = (r.get("Владелец") or "").strip() or cars.get(gos, {}).get("owner", "?")
        res.append({"date": (r.get("Дата") or "").strip(), "gos": gos,
                    "driver": (r.get("Водитель") or "").strip(),
                    "amount": amt, "owner": owner})
    return res

def get_cash():
    res = []
    for r in read_sheet("Касса"):
        amt = _num(r.get("Сумма") or 0)
        if amt <= 0: continue
        res.append({"date": (r.get("Дата") or "").strip(),
                    "type": (r.get("Тип операции") or "").strip(),
                    "category": (r.get("Категория") or "").strip(),
                    "sum": amt, "who": (r.get("Получил из кассы") or "").strip()})
    return res

def get_balances():
    rows = read_sheet("Балансы")
    if not rows: return None
    last = rows[-1]
    return {"date": (last.get("Дата") or "").strip(),
            "card": _num(last.get("Карта (₽)") or 0),
            "account": _num(last.get("Счёт (₽)") or 0),
            "cash": _num(last.get("Нал (₽)") or 0),
            "total": _num(last.get("Всего (₽)") or 0)}
