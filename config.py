import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNERS = {}
for key, name in [("OWNER_AB", "Александр Борисович"), ("OWNER_ANNA", "Анна"), ("OWNER_DANIIL", "Даниил")]:
    v = os.getenv(key, "0") or "0"
    try:
        uid = int(v)
        if uid:
            OWNERS[uid] = name
    except Exception:
        pass
ANNA_ID = int(os.getenv("OWNER_ANNA", "0") or "0")
