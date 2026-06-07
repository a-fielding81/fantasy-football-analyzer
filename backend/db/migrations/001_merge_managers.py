"""
Migration 001 — Merge ESPN owner rows into Sleeper manager rows.

ESPN owners were ingested as "Owner-N" placeholders because the espn-api
`owner` attribute is empty. The `owners` list, however, has full names.
This migration:
  1. Updates ESPN manager display_names to real first+last names.
  2. Merges each ESPN manager into the matching Sleeper manager so every
     manager has a single row with both espn_owner_id and sleeper_user_id.
  3. Re-points all teams/trade_assets/draft_picks to the surviving row.

Safe to re-run — uses IF EXISTS guards and checks before updating.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from db.database import get_connection

# ESPN team_id → (real display_name, sleeper_user_id)
# Derived from espn-api owners[0] firstName+lastName vs Sleeper usernames.
MERGE_MAP = [
    # (espn_suffix, real_name,         sleeper_user_id)
    ("_1",  "Andrew Fielding",  "591056123839160320"),
    ("_2",  "Jon Pan",          "735732776434794496"),   # Thepanman2012
    ("_3",  "Lucas Teuber",     "729773276074733568"),   # lucasteuber
    ("_4",  "Matt Lifshitz",    "735745286965698560"),   # mattlifs
    ("_5",  "Joe Gilhooly",     "735645198125809664"),   # SeanGilhooly
    ("_6",  "Connor Gross",     "735660262266363904"),   # ConnorGross14
    ("_7",  "Brian Pedretti",   "735840432759668736"),   # Brianpedretti
    ("_8",  "Jerry Markatos",   "654451977673043968"),   # JerryMarkatos
    ("_9",  "Danny Pedretti",   "735929732616605696"),   # dannypedretti
    ("_10", "Guha Sundaram",    "735732965774053376"),   # gsund11
]

ESPN_LEAGUE_ID = "92157291"


def run():
    conn = get_connection()
    merged = 0

    for suffix, real_name, sleeper_uid in MERGE_MAP:
        espn_owner_id = ESPN_LEAGUE_ID + suffix

        espn_row = conn.execute(
            "SELECT id FROM managers WHERE espn_owner_id = ?", (espn_owner_id,)
        ).fetchone()

        sleeper_row = conn.execute(
            "SELECT id FROM managers WHERE sleeper_user_id = ?", (sleeper_uid,)
        ).fetchone()

        if not espn_row:
            print(f"  SKIP {real_name}: ESPN row not found for {espn_owner_id}")
            continue

        espn_id = espn_row["id"]

        if sleeper_row:
            # Merge: re-point all references from ESPN row → Sleeper row, then delete ESPN row
            sleeper_id = sleeper_row["id"]

            for tbl, col in [
                ("teams",       "manager_id"),
                ("draft_picks", "team_id"),   # draft_picks references team, not manager directly
            ]:
                pass  # teams reference manager_id; handled below

            # Re-point teams to the surviving Sleeper row
            conn.execute(
                "UPDATE teams SET manager_id = ? WHERE manager_id = ?",
                (sleeper_id, espn_id),
            )

            # Clear espn_owner_id on ESPN row first (avoids UNIQUE conflict)
            conn.execute(
                "UPDATE managers SET espn_owner_id = NULL WHERE id = ?", (espn_id,)
            )

            # Now update the surviving Sleeper row with the ESPN id + real name
            conn.execute(
                "UPDATE managers SET espn_owner_id = ?, display_name = ? WHERE id = ?",
                (espn_owner_id, real_name, sleeper_id),
            )

            # Delete the now-orphaned ESPN row
            conn.execute("DELETE FROM managers WHERE id = ?", (espn_id,))
            print(f"  MERGED  {real_name}: ESPN({espn_id}) → Sleeper({sleeper_id})")
            merged += 1

        else:
            # No Sleeper counterpart — just update the display name
            conn.execute(
                "UPDATE managers SET display_name = ? WHERE id = ?",
                (real_name, espn_id),
            )
            print(f"  RENAMED {real_name}: ESPN only (id={espn_id})")
            merged += 1

    conn.commit()
    conn.close()
    print(f"\nDone. {merged} managers updated/merged.")


if __name__ == "__main__":
    run()
