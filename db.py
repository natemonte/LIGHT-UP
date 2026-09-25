"""Tiny SQLite helper layer -- no ORM, just clean wrappers.

Kept deliberately dependency-free (Python's stdlib sqlite3 only) so this
app has zero install steps beyond `pip install flask requests`.
"""
import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Where the database (and the Flask secret key file) live. Locally this is
# just a "data" folder next to the code. On a real host, set the CRM_DATA_DIR
# environment variable to point at a mounted persistent volume instead (e.g.
# "/data" on Railway) -- otherwise your database gets wiped on every
# redeploy, since a container's own filesystem doesn't survive that.
DATA_DIR = os.environ.get("CRM_DATA_DIR") or os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "crm.db")
SCHEMA_PATH = os.path.join(BASE_DIR, "schema.sql")


def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    with open(SCHEMA_PATH) as f:
        conn.executescript(f.read())
    conn.commit()
    conn.close()
    _run_migrations()


def _run_migrations():
    """Lightweight, additive-only migrations for columns added after the
    initial schema. Safe to run every startup -- each checks before altering."""
    conn = get_db()
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]
    if "public_token" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN public_token TEXT")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_public_token ON jobs(public_token)"
        )
    if "takedown_date" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN takedown_date TEXT")
    if "takedown_completed_date" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN takedown_completed_date TEXT")
    if "footage" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN footage REAL")
    if "price_per_foot" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN price_per_foot REAL")
    conn.commit()
    conn.close()


def query(sql, params=(), one=False):
    conn = get_db()
    cur = conn.execute(sql, params)
    rows = cur.fetchall()
    conn.close()
    if one:
        return rows[0] if rows else None
    return rows


def execute(sql, params=()):
    """Run an INSERT/UPDATE/DELETE, return the new row id (for INSERT)."""
    conn = get_db()
    cur = conn.execute(sql, params)
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def get_setting(key, default=None):
    row = query("SELECT value FROM settings WHERE key = ?", (key,), one=True)
    return row["value"] if row else default


def set_setting(key, value):
    execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
