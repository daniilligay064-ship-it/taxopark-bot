# -*- coding: utf-8 -*-
import csv
import io
import re
import urllib.request
import urllib.parse

# ID твоей таблицы (из ссылки на редактирование)
SPREADSHEET_ID = "1Gy7kdaABWojG5EyEENZio98ofp5t_yqipUt1g_VXmdc"

def _num(v):
    if v is None or v == "":
        return 0.0
    s = str(v).replace("\xa0", "").replace(" ", "").replace("₽", "").replace("р.", "")
    s = re.sub(r"[^\d.\-]", "", s.replace(",", "."))
    try:
        return float(s)
    except:
        return 0.0

def read_sheet(sheet_name):
    """Читает лист по его НАЗВАНИЮ, а не по gid."""
    sheet_encoded = urllib.parse.quote(sheet_name)
    url = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/gviz/tq?tqx=out:csv&sheet={sheet_encoded}"
    
    try:
        resp = urllib.request.urlopen(url, timeout=30)
        data = resp.read().decode("utf-8")
        
        if "<html" in data[:100].lower() or "<!doctype" in data[:100].lower():
            print(f"Ошибка: Google вернул HTML вместо CSV для листа {sheet_name}")
            return []
            
        return list(csv.DictReader(io.StringIO(data)))
    except Exception as e:
        print(f"Ошибка чтения листа {sheet_name}: {e}")
        return []

def get_cars():
    rows = read_sheet("Автомобили")
    cars = {}
    for r in rows:
        gos = (r.get("Госномер") or "").strip().upper()
        if not gos:
            continue
        owner = (r.get("Владелец") or "").strip() or "Александр Борисович"
        brand = (r.get("Марка") or "").strip()
        model = (r.get("Модель") or "").strip()
        cars[gos] = {"gos": gos, "owner": owner, "brand": brand, "model": model}
    return cars

def get_earnings():
    rows = read_sheet("Ввод данных")
    cars = get_cars()
    result = []
    for r in rows:
        date_v = (r.get("Дата") or "").strip()
        gos = (r.get("Госномер") or "").strip().upper()
        driver = (r.get("Водитель") or "").strip()
        amount = _num(r.get("Заработок") or 0)
        owner_col = (r.get("Владелец") or "").strip()
        if not date_v or not gos or amount <= 0:
            continue
        owner = owner_col
        if not owner:
            if gos in cars:
                owner = cars[gos]["owner"]
            else:
                owner = "?"
        result.append({
            "date": date_v,
            "gos": gos,
            "driver": driver,
            "amount": amount,
            "owner": owner,
        })
    return result

def get_cash():
    rows = read_sheet("Касса")
    result = []
    for r in rows:
        date_v = (r.get("Дата") or "").strip()
        op_type = (r.get("Тип операции") or "").strip()
        amount = _num(r.get("Сумма") or 0)
        if not date_v or amount <= 0:
            continue
        result.append({
            "date": date_v,
            "type": op_type,
            "category": (r.get("Категория") or "").strip(),
            "sum": amount,
            "account": (r.get("Счёт") or "").strip(),
            "who": (r.get("Получил из кассы") or "").strip(),
            "comment": (r.get("Комментарий") or "").strip(),
        })
    return result

def get_balances():
    rows = read_sheet("Балансы")
    if not rows:
        return None
    last = rows[-1]
    return {
        "date": (last.get("Дата") or "").strip(),
        "card": _num(last.get("Карта (₽)") or 0),
        "account": _num(last.get("Счёт (₽)") or 0),
        "cash": _num(last.get("Нал (₽)") or 0),
        "total": _num(last.get("Всего (₽)") or 0),
    }
