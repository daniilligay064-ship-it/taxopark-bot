# -*- coding: utf-8 -*-
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from paths import DB_PATH, BACKUP_DIR
logger = logging.getLogger(__name__)


def copy_database(source, target):
    """Копирование через SQLite Backup API + проверка целостности."""
    with closing(sqlite3.connect(str(source), timeout=30)) as src, \
         closing(sqlite3.connect(str(target), timeout=30)) as dst:
        src.backup(dst, pages=256, sleep=0.1)
        result = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise sqlite3.DatabaseError(f"Проверка копии не пройдена: {result}")


def backup_database():
    """Ежедневный бэкап с хранением 7 последних копий."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    target = BACKUP_DIR / f"taxopark_{stamp}.db"
    temp = BACKUP_DIR / f".taxopark_{stamp}.tmp.db"
    try:
        if temp.exists():
            temp.unlink()
        copy_database(DB_PATH, temp)
        os.replace(temp, target)
        backups = sorted(
            BACKUP_DIR.glob("taxopark_????????.db"),
            key=lambda p: p.name,
            reverse=True,
        )
        for old in backups[7:]:
            old.unlink()
        logger.info("Бэкап создан: %s", target)
        return str(target)
    finally:
        if temp.exists():
            temp.unlink()


def list_backups():
    if not BACKUP_DIR.exists():
        return []
    return sorted(BACKUP_DIR.glob("taxopark_????????.db"), key=lambda p: p.name, reverse=True)