"""SQLite storage: one row per snapshot, a URL->hash cache, and cached LLM notes."""
from __future__ import annotations

import json
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY,
    store TEXT NOT NULL,
    app_id TEXT NOT NULL,
    country TEXT NOT NULL,
    taken_at TEXT NOT NULL,
    title TEXT, subtitle TEXT, developer TEXT, description TEXT,
    version TEXT, version_date TEXT, release_notes TEXT,
    price REAL, currency TEXT, has_iap INTEGER,
    rating REAL, rating_count INTEGER,
    screenshots TEXT NOT NULL,   -- JSON list of {url, hash}
    url TEXT
);
CREATE INDEX IF NOT EXISTS snapshots_app ON snapshots(store, app_id, country, taken_at);
CREATE TABLE IF NOT EXISTS image_hashes (url TEXT PRIMARY KEY, hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notes (
    from_id INTEGER NOT NULL, to_id INTEGER NOT NULL,
    model TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (from_id, to_id)
);
"""

FIELDS = ["store", "app_id", "country", "taken_at", "title", "subtitle", "developer", "description",
          "version", "version_date", "release_notes", "price", "currency", "has_iap",
          "rating", "rating_count", "screenshots", "url"]


def connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def save_snapshot(con: sqlite3.Connection, snap: dict) -> int:
    row = dict(snap)
    row["screenshots"] = json.dumps(row.get("screenshots") or [])
    if row.get("has_iap") is not None:
        row["has_iap"] = int(bool(row["has_iap"]))
    cur = con.execute(
        f"INSERT INTO snapshots ({', '.join(FIELDS)}) VALUES ({', '.join('?' for _ in FIELDS)})",
        [row.get(f) for f in FIELDS],
    )
    con.commit()
    return cur.lastrowid


def _to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["screenshots"] = json.loads(d["screenshots"])
    return d


def history(con: sqlite3.Connection, store: str, app_id: str, country: str) -> list[dict]:
    rows = con.execute(
        "SELECT * FROM snapshots WHERE store=? AND app_id=? AND country=? ORDER BY taken_at, id",
        (store, app_id, country),
    )
    return [_to_dict(r) for r in rows]


def tracked(con: sqlite3.Connection) -> list[tuple[str, str, str]]:
    return [tuple(r) for r in con.execute("SELECT DISTINCT store, app_id, country FROM snapshots ORDER BY 1, 2, 3")]


def cached_hash(con: sqlite3.Connection, url: str) -> str | None:
    row = con.execute("SELECT hash FROM image_hashes WHERE url=?", (url,)).fetchone()
    return row[0] if row else None


def store_hash(con: sqlite3.Connection, url: str, value: str) -> None:
    con.execute("INSERT OR REPLACE INTO image_hashes (url, hash) VALUES (?, ?)", (url, value))
    con.commit()


def get_note(con: sqlite3.Connection, from_id: int, to_id: int) -> dict | None:
    row = con.execute("SELECT model, note, created_at FROM notes WHERE from_id=? AND to_id=?", (from_id, to_id)).fetchone()
    return dict(row) if row else None


def put_note(con: sqlite3.Connection, from_id: int, to_id: int, model: str, note: str, created_at: str) -> None:
    con.execute("INSERT OR REPLACE INTO notes VALUES (?, ?, ?, ?, ?)", (from_id, to_id, model, note, created_at))
    con.commit()
