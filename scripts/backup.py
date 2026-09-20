#!/usr/bin/env python3
"""Copy the live database to a timestamped backup file.

Run manually any time, or on a schedule (recommended: daily). Most hosts
(Railway, Render, etc.) let you run a "cron job" alongside your web app --
point it at this script. Keeps the last 30 backups automatically.

Usage: python3 scripts/backup.py
"""
import os
import shutil
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db as dbmodule  # reuse the same CRM_DATA_DIR-aware path db.py uses

DB_PATH = dbmodule.DB_PATH
BACKUP_DIR = os.path.join(dbmodule.DATA_DIR, "backups")
KEEP_LAST = 30


def main():
    if not os.path.exists(DB_PATH):
        print(f"No database found at {DB_PATH}, nothing to back up.")
        sys.exit(0)

    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    dest = os.path.join(BACKUP_DIR, f"crm_{stamp}.db")
    shutil.copy2(DB_PATH, dest)
    print(f"Backed up to {dest}")

    backups = sorted(
        f for f in os.listdir(BACKUP_DIR) if f.startswith("crm_") and f.endswith(".db")
    )
    for old in backups[:-KEEP_LAST]:
        os.remove(os.path.join(BACKUP_DIR, old))


if __name__ == "__main__":
    main()
