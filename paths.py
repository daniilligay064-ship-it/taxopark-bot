# -*- coding: utf-8 -*-
"""Общие пути проекта. Работает и на Windows, и на Linux."""
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
LOG_DIR = BASE / "logs"
BACKUP_DIR = DATA_DIR / "backups"

DB_PATH = DATA_DIR / "taxopark.db"
DB = str(DB_PATH)

# Создать папки, если их нет
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)