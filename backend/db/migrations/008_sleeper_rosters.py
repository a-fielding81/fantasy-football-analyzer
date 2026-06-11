"""
Migration 008 — Ingest end-of-season Sleeper rosters.

The original Sleeper ingestion only populated roster_players for ESPN seasons.
This migration pulls /league/{id}/rosters for each historical Sleeper season
and stores all players as week=0 (end-of-season snapshot) rows.
"""

import sys
import json
import urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from db.database import get_connection

def fetch(url):
    with urllib.request.urlopen(url) as r:
        return json.loads(r.read())

def run():
    conn = get_connection()

    sleeper_seasons = conn.execute(
        "SELECT id, year, platform_season_id FROM seasons WHERE platform='sleeper' ORDER BY year"
    ).fetchall()

    # Build sleeper_id → db player id map
    player_map = {}
    for row in conn.execute("SELECT id, sleeper_id FROM players WHERE sleeper_id IS NOT NULL"):
        player_map[str(row["sleeper_id"])] = row["id"]
    print(f"Player map: {len(player_map)} entries")

    total = 0
    for season in sleeper_seasons:
        lid = season["platform_season_id"]
        year = season["year"]
        season_id = season["id"]

        try:
            rosters = fetch(f"https://api.sleeper.app/v1/league/{lid}/rosters")
        except Exception as e:
            print(f"  {year}: failed to fetch rosters: {e}")
            continue

        # Build owner_id → team_id map for this season
        team_map = {}
        for row in conn.execute("""
            SELECT t.id, m.sleeper_user_id
            FROM teams t
            JOIN managers m ON m.id = t.manager_id
            WHERE t.season_id = ?
        """, (season_id,)):
            if row["sleeper_user_id"]:
                team_map[str(row["sleeper_user_id"])] = row["id"]

        inserted = skipped = 0
        for roster in rosters:
            owner_id = str(roster.get("owner_id") or "")
            team_id = team_map.get(owner_id)
            if not team_id:
                continue

            players = roster.get("players") or []
            for sleeper_pid in players:
                db_pid = player_map.get(str(sleeper_pid))
                if not db_pid:
                    skipped += 1
                    continue
                try:
                    conn.execute("""
                        INSERT OR IGNORE INTO roster_players
                            (season_id, team_id, player_id, week, acquisition_type)
                        VALUES (?, ?, ?, 0, 'unknown')
                    """, (season_id, team_id, db_pid))
                    inserted += 1
                except Exception as e:
                    print(f"  Error: {e}")

        conn.commit()
        total += inserted
        print(f"  {year}: {inserted} players inserted, {skipped} sleeper_ids not matched")

    print(f"\nTotal: {total} roster_player rows added")
    conn.close()

if __name__ == "__main__":
    run()
