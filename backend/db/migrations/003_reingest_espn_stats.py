"""
Migration 003 — Re-ingest ESPN player weekly stats.

The original ingestion missed player_weekly_stats for ESPN seasons because
player IDs weren't resolving correctly from box score lineup objects.
We now look up by name (fallback) and by playerId, and correctly store
player-level fantasy point totals per week.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from espn_api.football import League
from db.database import get_connection

ESPN_LEAGUE_ID = 92157291
ESPN_YEARS = [2020, 2021, 2022]


def build_player_map(conn) -> tuple[dict, dict]:
    """Return (espn_id → db_player_id, lower_name → db_player_id)."""
    by_espn: dict[str, int] = {}
    by_name: dict[str, int] = {}
    rows = conn.execute("SELECT id, espn_id, full_name FROM players").fetchall()
    for r in rows:
        if r["espn_id"]:
            by_espn[str(r["espn_id"])] = r["id"]
        by_name[r["full_name"].lower()] = r["id"]
    return by_espn, by_name


def run():
    conn = get_connection()
    by_espn, by_name = build_player_map(conn)

    for year in ESPN_YEARS:
        season_row = conn.execute(
            "SELECT id FROM seasons WHERE platform='espn' AND year=?", (year,)
        ).fetchone()
        if not season_row:
            print(f"  {year}: no season row found, skipping")
            continue
        season_id = season_row["id"]

        print(f"\n  === ESPN {year} ===")
        try:
            league = League(league_id=ESPN_LEAGUE_ID, year=year)
        except Exception as e:
            print(f"  Failed to load league: {e}")
            continue

        total_rows = 0
        for week in range(1, 18):
            try:
                box_scores = league.box_scores(week)
            except Exception:
                break

            for box in box_scores:
                for lineup in [box.home_lineup, box.away_lineup]:
                    if not lineup:
                        continue
                    for player in lineup:
                        pts = getattr(player, "points", 0) or 0
                        if pts == 0:
                            continue

                        # Try playerId first, then name
                        pid = str(getattr(player, "playerId", "") or "")
                        db_player_id = by_espn.get(pid)
                        if not db_player_id:
                            name = (getattr(player, "name", None) or "").lower()
                            db_player_id = by_name.get(name)
                        if not db_player_id:
                            continue

                        conn.execute("""
                            INSERT INTO player_weekly_stats
                                (season_id, player_id, week, fantasy_points)
                            VALUES (?, ?, ?, ?)
                            ON CONFLICT (season_id, player_id, week)
                            DO UPDATE SET fantasy_points = MAX(fantasy_points, excluded.fantasy_points)
                        """, (season_id, db_player_id, week, pts))
                        total_rows += 1

            conn.commit()

        print(f"  Inserted/updated {total_rows} player-week stat rows for {year}")

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    run()
