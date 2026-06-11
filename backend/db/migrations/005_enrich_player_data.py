"""
Migration 005 — Enrich player records from Sleeper players API.

Adds to players table:
  - depth_chart_order  (1=starter, 2=backup, NULL=unknown)
  - search_rank        (Sleeper dynasty/redraft rank — lower = more valuable)
  - height             (inches)
  - weight             (lbs)
  - college
  - gsis_id            (NFL gsis ID — used to join with nflverse stats)
  - years_exp          (already in schema, fill blanks)
  - birth_date         (already in schema, fill blanks)
  - status             (already in schema, fill blanks)

Matches by sleeper_id. Players without a sleeper_id are skipped.
"""

import sys
import json
import urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from db.database import get_connection

SLEEPER_PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"

NEW_COLUMNS = [
    ("depth_chart_order", "INTEGER"),
    ("search_rank",       "INTEGER"),
    ("height",            "INTEGER"),
    ("weight",            "INTEGER"),
    ("college",           "TEXT"),
    ("gsis_id",           "TEXT"),
]


def add_columns_if_missing(conn):
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(players)")}
    for col, col_type in NEW_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE players ADD COLUMN {col} {col_type}")
            print(f"  Added column: players.{col}")
    conn.commit()


def run():
    conn = get_connection()
    add_columns_if_missing(conn)

    print("Fetching Sleeper players (~12k records)…")
    with urllib.request.urlopen(SLEEPER_PLAYERS_URL) as r:
        all_players = json.loads(r.read())
    print(f"  {len(all_players)} players fetched")

    # Build lookup: sleeper_id → player data
    by_sleeper_id = {str(p["player_id"]): p for p in all_players.values() if p.get("player_id")}

    # Fetch all our players that have a sleeper_id
    db_players = conn.execute(
        "SELECT id, sleeper_id FROM players WHERE sleeper_id IS NOT NULL"
    ).fetchall()
    print(f"  {len(db_players)} players in DB with sleeper_id")

    updated = 0
    skipped = 0
    for row in db_players:
        sid = str(row["sleeper_id"])
        sp = by_sleeper_id.get(sid)
        if not sp:
            skipped += 1
            continue

        gsis_raw = sp.get("gsis_id") or ""
        gsis = gsis_raw.strip() if gsis_raw else None

        conn.execute("""
            UPDATE players SET
                birth_date         = COALESCE(birth_date, ?),
                years_exp          = COALESCE(years_exp, ?),
                status             = ?,
                depth_chart_order  = ?,
                search_rank        = ?,
                height             = ?,
                weight             = ?,
                college            = COALESCE(college, ?),
                gsis_id            = COALESCE(gsis_id, ?)
            WHERE id = ?
        """, (
            sp.get("birth_date"),
            sp.get("years_exp"),
            sp.get("status"),
            sp.get("depth_chart_order"),
            sp.get("search_rank"),
            sp.get("height"),
            sp.get("weight"),
            sp.get("college"),
            gsis,
            row["id"],
        ))
        updated += 1

    conn.commit()
    print(f"  Updated {updated} players, skipped {skipped}")

    # Spot-check
    print("\nSpot-check (key trade players):")
    rows = conn.execute("""
        SELECT full_name, position, birth_date, years_exp, status,
               depth_chart_order, search_rank, gsis_id
        FROM players
        WHERE full_name IN ('Jonathan Taylor','Dalvin Cook','DK Metcalf',
                            'Bijan Robinson','Justin Jefferson','Josh Allen')
        ORDER BY search_rank
    """).fetchall()
    for r in rows:
        print(f"  {r['full_name']:<22} {r['position']} born={r['birth_date']} "
              f"exp={r['years_exp']} rank={r['search_rank']} gsis={r['gsis_id']}")

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    run()
