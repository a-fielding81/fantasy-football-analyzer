"""
Migration 006 — Ingest nflverse season-level advanced stats (2020–2024).

Creates player_season_advanced table with per-season opportunity and
efficiency metrics needed for trade grading:
  - games played (durability)
  - carries, rushing_yards, rushing_tds, rushing_epa      (RB opportunity)
  - targets, receptions, receiving_yards, receiving_tds   (WR/TE opportunity)
  - target_share, air_yards_share, wopr                   (role quality)
  - receiving_epa, rushing_epa                            (efficiency)
  - fantasy_points_ppr                                    (nflverse ground-truth)

Match strategy:
  1. Primary: nflverse gsis_id → players.gsis_id
  2. Fallback: nflverse player_display_name (full name) → players.full_name
     + update players.gsis_id for future joins

Source: https://github.com/nflverse/nflverse-data
"""

import sys
import csv
import io
import urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from db.database import get_connection

YEARS = [2020, 2021, 2022, 2023, 2024]
URL_TMPL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "player_stats/player_stats_season_{year}.csv"
)

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS player_season_advanced (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id           INTEGER NOT NULL REFERENCES players(id),
    season_year         INTEGER NOT NULL,
    games               INTEGER,
    -- Rushing
    carries             INTEGER,
    rushing_yards       REAL,
    rushing_tds         INTEGER,
    rushing_epa         REAL,
    -- Receiving
    targets             INTEGER,
    receptions          INTEGER,
    receiving_yards     REAL,
    receiving_tds       INTEGER,
    receiving_epa       REAL,
    -- Opportunity share metrics
    target_share        REAL,   -- fraction of team targets
    air_yards_share     REAL,   -- fraction of team air yards
    wopr                REAL,   -- weighted opportunity rating
    -- Ground-truth fantasy total (PPR, from nflverse)
    fantasy_points_ppr  REAL,
    -- Source identifier
    gsis_id             TEXT,
    UNIQUE (player_id, season_year)
);
"""


def safe_float(val):
    try:
        return float(val) if val not in (None, "", "NA", "nan") else None
    except (ValueError, TypeError):
        return None


def safe_int(val):
    try:
        f = safe_float(val)
        return int(f) if f is not None else None
    except (ValueError, TypeError):
        return None


def ingest_row(conn, db_pid, gsis, year, row):
    conn.execute("""
        INSERT INTO player_season_advanced (
            player_id, season_year, games,
            carries, rushing_yards, rushing_tds, rushing_epa,
            targets, receptions, receiving_yards, receiving_tds, receiving_epa,
            target_share, air_yards_share, wopr,
            fantasy_points_ppr, gsis_id
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT (player_id, season_year)
        DO UPDATE SET
            games               = excluded.games,
            carries             = excluded.carries,
            rushing_yards       = excluded.rushing_yards,
            rushing_tds         = excluded.rushing_tds,
            rushing_epa         = excluded.rushing_epa,
            targets             = excluded.targets,
            receptions          = excluded.receptions,
            receiving_yards     = excluded.receiving_yards,
            receiving_tds       = excluded.receiving_tds,
            receiving_epa       = excluded.receiving_epa,
            target_share        = excluded.target_share,
            air_yards_share     = excluded.air_yards_share,
            wopr                = excluded.wopr,
            fantasy_points_ppr  = excluded.fantasy_points_ppr,
            gsis_id             = excluded.gsis_id
    """, (
        db_pid, year,
        safe_int(row.get("games")),
        safe_int(row.get("carries")),
        safe_float(row.get("rushing_yards")),
        safe_int(row.get("rushing_tds")),
        safe_float(row.get("rushing_epa")),
        safe_int(row.get("targets")),
        safe_int(row.get("receptions")),
        safe_float(row.get("receiving_yards")),
        safe_int(row.get("receiving_tds")),
        safe_float(row.get("receiving_epa")),
        safe_float(row.get("target_share")),
        safe_float(row.get("air_yards_share")),
        safe_float(row.get("wopr")),
        safe_float(row.get("fantasy_points_ppr")),
        gsis,
    ))


def run():
    conn = get_connection()
    conn.execute(CREATE_TABLE)
    conn.commit()
    print("Table player_season_advanced ready.")

    # Build lookup maps from our DB
    gsis_map: dict[str, int] = {}       # gsis_id  → db player_id
    name_map: dict[str, int] = {}       # lower full_name → db player_id
    for row in conn.execute(
        "SELECT id, gsis_id, full_name FROM players"
    ):
        name_map[row["full_name"].lower()] = row["id"]
        if row["gsis_id"]:
            gsis_map[row["gsis_id"].strip()] = row["id"]

    print(f"  {len(gsis_map)} players with gsis_id | {len(name_map)} total players")

    total_inserted = 0
    total_name_matched = 0
    total_skipped = 0
    new_gsis_updates = 0

    for year in YEARS:
        url = URL_TMPL.format(year=year)
        print(f"\n=== {year} ===")
        try:
            with urllib.request.urlopen(url) as r:
                content = r.read().decode("utf-8")
        except Exception as e:
            print(f"  Failed to fetch: {e}")
            continue

        reader = csv.DictReader(io.StringIO(content))
        inserted = name_matched = skipped = 0

        for row in reader:
            if row.get("season_type") != "REG":
                continue

            gsis = (row.get("player_id") or "").strip()
            db_pid = gsis_map.get(gsis)

            if db_pid:
                # Primary match via gsis_id
                try:
                    ingest_row(conn, db_pid, gsis, year, row)
                    inserted += 1
                except Exception as e:
                    print(f"  Error {row.get('player_display_name')}: {e}")
            else:
                # Fallback: match by full name
                display_name = (row.get("player_display_name") or "").lower()
                db_pid = name_map.get(display_name)
                if db_pid:
                    try:
                        ingest_row(conn, db_pid, gsis, year, row)
                        name_matched += 1
                        # Update gsis_id on the player record so future runs use primary match
                        if gsis:
                            conn.execute(
                                "UPDATE players SET gsis_id = ? WHERE id = ? AND gsis_id IS NULL",
                                (gsis, db_pid)
                            )
                            gsis_map[gsis] = db_pid  # add to cache
                            new_gsis_updates += 1
                    except Exception as e:
                        print(f"  Error {row.get('player_display_name')}: {e}")
                else:
                    skipped += 1

        conn.commit()
        total_inserted += inserted
        total_name_matched += name_matched
        total_skipped += skipped
        print(f"  gsis match: {inserted}  name fallback: {name_matched}  "
              f"skipped: {skipped}  gsis_ids written: {new_gsis_updates}")

    print(f"\nTotal: {total_inserted} gsis-matched + {total_name_matched} name-matched "
          f"= {total_inserted + total_name_matched} rows | {total_skipped} unmatched")

    # Coverage report
    print("\nCoverage by position (players in our DB with ≥1 advanced stat row):")
    rows = conn.execute("""
        SELECT p.position, COUNT(DISTINCT p.id) as players_with_stats
        FROM players p
        JOIN player_season_advanced psa ON psa.player_id = p.id
        GROUP BY p.position ORDER BY players_with_stats DESC
    """).fetchall()
    for r in rows:
        print(f"  {r['position']}: {r['players_with_stats']}")

    # Spot-check key trade players
    print("\nSpot-check — key players across seasons:")
    check_players = [
        "Jonathan Taylor", "Dalvin Cook", "DK Metcalf",
        "Bijan Robinson", "Justin Jefferson", "Derrick Henry"
    ]
    for name in check_players:
        rows = conn.execute("""
            SELECT psa.season_year, psa.games, psa.carries, psa.rushing_yards,
                   psa.targets, psa.target_share, psa.wopr, psa.fantasy_points_ppr
            FROM player_season_advanced psa
            JOIN players p ON p.id = psa.player_id
            WHERE p.full_name = ? AND p.position IN ('RB','WR','QB','TE')
            ORDER BY psa.season_year
        """, (name,)).fetchall()
        if rows:
            print(f"  {name}:")
            for r in rows:
                tgt_share = f"{r['target_share']:.1%}" if r['target_share'] else "—"
                print(f"    {r['season_year']}: {r['games']}g  car={r['carries']}  "
                      f"tgt={r['targets']}({tgt_share})  {r['fantasy_points_ppr']}pts(ppr)")
        else:
            print(f"  {name}: NO DATA")

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    run()
