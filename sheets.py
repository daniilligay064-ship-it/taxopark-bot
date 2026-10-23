import re, csv, io, json
import urllib.request
from database import save_cache, get_cache
from config import PUB_ID

BASE_PUB = "https://docs.google.com/spreadsheets/d/e/" + PUB_ID + "/pub"

def discover_sheets():
    html = urllib.request.urlopen(BASE_PUB + "html", timeout=25).read().decode("utf-8")
    pattern = re.compile(r"name:\s*\"([^\"]+)\"[\s\S]{0,600}?gid:\s*\"(\d+)\"")
    found = {}
    for name, gid in pattern.findall(html):
        if name not in found:
            found[name] = gid
    return found

def read_sheet(gid):
    url = BASE_PUB + "?gid=" + gid + "&single=true&output=csv"
    data = urllib.request.urlopen(url, timeout=25).read().decode("utf-8")
    reader = csv.DictReader(io.StringIO(data))
    return [row for row in reader]

def get_all_sheets(force=False):
    if not force:
        cached = get_cache("__all__")
        if cached:
            return json.loads(cached)
    result = {}
    sheets = discover_sheets()
    for name, gid in sheets.items():
        try:
            result[name] = read_sheet(gid)
        except Exception as e:
            result[name] = []
    save_cache("__all__", json.dumps(result, ensure_ascii=False))
    return result