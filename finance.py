from datetime import datetime

CREDIT_AB = 21150
ANNA_FEE = 666

def parse_date(s):
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except:
            pass
    return None

def calc_day(earnings, date_str):
    target = parse_date(date_str)
    if not target:
        return None
    by = {"Александр Борисович": 0.0, "Даниил": 0.0, "Анна": 0.0}
    details = []
    for e in earnings:
        d = parse_date(e["date"])
        if d == target:
            o = e["owner"]
            if o in by:
                by[o] += e["amount"]
            details.append(e)
    ab, dan, anna = by["Александр Борисович"], by["Даниил"], by["Анна"]
    return {
        "date": target.isoformat(),
        "ab_gross": ab, "dan_gross": dan, "anna_gross": anna,
        "ab_net": ab - CREDIT_AB - ANNA_FEE,
        "dan_net": dan - ANNA_FEE,
        "anna_total": anna + ANNA_FEE * 2,
        "total": ab + dan + anna,
        "count": len(details),
    }

def calc_period(earnings, date_from, date_to):
    d1, d2 = parse_date(date_from), parse_date(date_to)
    if not d1 or not d2:
        return None
    days = {}
    for e in earnings:
        d = parse_date(e["date"])
        if not d or d < d1 or d > d2:
            continue
        key = d.isoformat()
        if key not in days:
            days[key] = {"ab": 0.0, "dan": 0.0, "anna": 0.0, "count": 0}
        o = e["owner"]
        if o == "Александр Борисович":
            days[key]["ab"] += e["amount"]
        elif o == "Даниил":
            days[key]["dan"] += e["amount"]
        elif o == "Анна":
            days[key]["anna"] += e["amount"]
        days[key]["count"] += 1
    if not days:
        return None
    n = len(days)
    sa = sum(x["ab"] for x in days.values())
    sd = sum(x["dan"] for x in days.values())
    sn = sum(x["anna"] for x in days.values())
    return {
        "from": d1.isoformat(), "to": d2.isoformat(), "days": n,
        "ab_gross": sa, "dan_gross": sd, "anna_gross": sn,
        "ab_net": sa - CREDIT_AB * n - ANNA_FEE * n,
        "dan_net": sd - ANNA_FEE * n,
        "anna_total": sn + ANNA_FEE * 2 * n,
        "total": sa + sd + sn,
        "count": sum(x["count"] for x in days.values()),
        "by_day": days,
    }

def fmt(n):
    return f"{int(round(n)):,}".replace(",", " ")
