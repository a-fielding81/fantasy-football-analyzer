"""
Migration 004 — Backfill is_keeper flag on Sleeper draft picks.

Sleeper's /draft/{id}/picks endpoint has an `is_keeper` field (True/None)
per pick. Our original ingestion stored it as 0 for all picks.
This migration re-fetches draft picks for every Sleeper season and sets
is_keeper = 1 on the correct rows.
"""

import sys
import json
import urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from db.database import get_connection


def fetch(url: str):
    with urllib.request.urlopen(url) as r:
        return json.loads(r.read())


def run():
    conn = get_connection()

    sleeper_seasons = conn.execute(
        "SELECT id, year, platform_season_id FROM seasons WHERE platform='sleeper' ORDER BY year"
    ).fetchall()

    for season in sleeper_seasons:
        season_id   = season["id"]
        year        = season["year"]
        league_id   = season["platform_season_id"]

        print(f"\n=== Sleeper {year} (league {league_id}) ===")

        try:
            drafts = fetch(f"https://api.sleeper.app/v1/league/{league_id}/drafts")
        except Exception as e:
            print(f"  Could not fetch drafts: {e}")
            continue

        if not drafts:
            print(f"  No drafts found, skipping")
            continue

        draft_id = drafts[0]["draft_id"]

        try:
            picks = fetch(f"https://api.sleeper.app/v1/draft/{draft_id}/picks")
        except Exception as e:
            print(f"  Could not fetch picks: {e}")
            continue

        keeper_player_ids = {
            p["player_id"]
            for p in picks
            if p.get("is_keeper") is True
        }

        print(f"  {len(picks)} picks, {len(keeper_player_ids)} keeper player IDs from API")

        # Map Sleeper player_id → db player id
        updated = 0
        skipped = 0
        for sleeper_pid in keeper_player_ids:
            player_row = conn.execute(
                "SELECT id FROM players WHERE sleeper_id = ?", (str(sleeper_pid),)
            ).fetchone()
            if not player_row:
                skipped += 1
                continue
            db_player_id = player_row["id"]

            result = conn.execute(
                """UPDATE draft_picks
                   SET is_keeper = 1
                   WHERE season_id = ? AND player_id = ?""",
                (season_id, db_player_id),
            )
            if result.rowcount > 0:
                updated += 1
            else:
                skipped += 1

        conn.commit()
        print(f"  Updated {updated} picks to is_keeper=1 ({skipped} player IDs not matched in DB)")

    # Final summary
    print("\n=== Keeper counts after migration ===")
    rows = conn.execute("""
        SELECT s.year, COUNT(*) as total, SUM(dp.is_keeper) as keepers
        FROM draft_picks dp
        JOIN seasons s ON dp.season_id = s.id
        WHERE s.platform = 'sleeper'
        GROUP BY s.year ORDER BY s.year
    """).fetchall()
    for r in rows:
        print(f"  {r['year']}: {r['keepers']}/{r['total']} keepers")

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    run()
