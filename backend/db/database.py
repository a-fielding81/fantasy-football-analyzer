from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent / "data" / "fantasy.db"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db():
    conn = get_connection()
    with open(SCHEMA_PATH) as f:
        conn.executescript(f.read())
    conn.commit()
    conn.close()
    print(f"Database initialized at {DB_PATH}")


def upsert(conn: sqlite3.Connection, table: str, data: dict, conflict_cols: list[str]) -> int:
    """Generic upsert — insert or replace on conflict_cols. Returns rowid."""
    cols = list(data.keys())
    placeholders = ", ".join(["?"] * len(cols))
    conflict = ", ".join(conflict_cols)
    update_set = ", ".join(
        f"{c} = excluded.{c}" for c in cols if c not in conflict_cols
    )
    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({conflict}) DO UPDATE SET {update_set} "
        f"RETURNING id"
    )
    cur = conn.execute(sql, list(data.values()))
    row = cur.fetchone()
    return row[0] if row else conn.execute(
        f"SELECT id FROM {table} WHERE {' AND '.join(f'{c}=?' for c in conflict_cols)}",
        [data[c] for c in conflict_cols]
    ).fetchone()[0]
