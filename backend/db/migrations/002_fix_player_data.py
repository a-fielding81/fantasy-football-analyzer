"""
Migration 002 — Fix ESPN player data and deduplicate players table.

Problems addressed:
1. ESPN-only players have position=UNKNOWN because the espn-api lineup
   objects don't carry position. We name-match them to Sleeper players.
2. Many players have two rows: one with espn_id (no position) and one
   with sleeper_id (has position). We merge them into a single row and
   re-point all FK references.
3. After merging, ESPN seasons will point to the enriched player rows.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from db.database import get_connection


def run():
    conn = get_connection()
    conn.execute("PRAGMA foreign_keys = OFF")

    # Step 1: Find ESPN players that have a Sleeper counterpart by name
    # Merge: copy espn_id onto the Sleeper row, then re-point FKs to Sleeper row
    dupes = conn.execute("""
        SELECT
            esp.id    AS espn_player_id,
            slp.id    AS sleeper_player_id,
            esp.full_name,
            esp.espn_id,
            slp.position,
            slp.nfl_team
        FROM players esp
        JOIN players slp
            ON LOWER(esp.full_name) = LOWER(slp.full_name)
           AND slp.sleeper_id IS NOT NULL
        WHERE esp.espn_id IS NOT NULL
          AND esp.sleeper_id IS NULL
    """).fetchall()

    merged = 0
    for row in dupes:
        espn_pid    = row["espn_player_id"]
        sleeper_pid = row["sleeper_player_id"]
        espn_id     = row["espn_id"]

        try:
            # Clear any stale espn_id that might be on another row
            conn.execute("UPDATE players SET espn_id = NULL WHERE espn_id = ?", (espn_id,))
            # Set espn_id on the Sleeper row
            conn.execute("UPDATE players SET espn_id = ? WHERE id = ?", (espn_id, sleeper_pid))

            for tbl, col in [
                ("draft_picks",        "player_id"),
                ("roster_players",     "player_id"),
                ("trade_assets",       "player_id"),
                ("player_weekly_stats","player_id"),
            ]:
                conn.execute(
                    f"UPDATE {tbl} SET {col} = ? WHERE {col} = ?",
                    (sleeper_pid, espn_pid)
                )

            conn.execute("DELETE FROM players WHERE id = ?", (espn_pid,))
            merged += 1
        except Exception as e:
            print(f"  SKIP {row['full_name']}: {e}")
            conn.rollback()
            continue

    conn.commit()
    print(f"Merged {merged} duplicate player rows (ESPN+Sleeper → single row)")

    # Step 2: For any remaining ESPN-only players still UNKNOWN, try a broader match
    # (some names differ slightly between platforms — skip for now, just report)
    still_unknown = conn.execute("""
        SELECT COUNT(*) FROM players
        WHERE espn_id IS NOT NULL AND sleeper_id IS NULL AND position = 'UNKNOWN'
    """).fetchone()[0]
    print(f"Remaining ESPN-only UNKNOWN players (no Sleeper name match): {still_unknown}")

    # Step 3: Report final position distribution for ESPN-touched players
    rows = conn.execute("""
        SELECT position, COUNT(*) as cnt
        FROM players
        WHERE espn_id IS NOT NULL
        GROUP BY position
        ORDER BY cnt DESC
    """).fetchall()
    print("\nPosition breakdown for ESPN players after merge:")
    for r in rows:
        print(f"  {r['position']}: {r['cnt']}")

    conn.execute("PRAGMA foreign_keys = ON")
    conn.close()


if __name__ == "__main__":
    run()
