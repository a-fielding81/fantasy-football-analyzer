"""
Migration 007 — Pull full nflverse history (1999–2024) for aging curve computation.

Stores all seasons in player_season_advanced, keyed by gsis_id for players
already in our DB, and in a standalone nfl_player_seasons table for everyone
else (needed for aging curves across the full population, not just our players).

nfl_player_seasons schema mirrors player_season_advanced but adds:
  - display_name, position (denormalized — no FK to our players table)
  - birth_year (derived from Sleeper cross-reference where possible)

This gives us ~26 seasons × ~600 skill players/season ≈ 15,000+ rows to
fit proper aging curves with real statistical power.
"""

import sys
import csv
import io
import json
import urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from db.database import get_connection

ALL_YEARS = list(range(1999, 2025))   # 1999–2024

URL_TMPL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "player_stats/player_stats_season_{year}.csv"
)
SLEEPER_PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"

CREATE_NFL_SEASONS = """
CREATE TABLE IF NOT EXISTS nfl_player_seasons (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    gsis_id             TEXT    NOT NULL,
    season_year         INTEGER NOT NULL,
    display_name        TEXT,
    position            TEXT,
    team                TEXT,
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
    -- Opportunity share
    target_share        REAL,
    air_yards_share     REAL,
    wopr                REAL,
    -- Production
    fantasy_points_ppr  REAL,
    -- Bio (filled from Sleeper cross-ref)
    birth_date          TEXT,
    UNIQUE (gsis_id, season_year)
);
"""

CREATE_AGING_CURVE = """
CREATE TABLE IF NOT EXISTS aging_curves (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    position    TEXT    NOT NULL,
    age         INTEGER NOT NULL,
    avg_pts_ppr REAL,
    yoy_delta   REAL,       -- average change from age-1 to this age
    delta_pct   REAL,       -- yoy_delta / avg_pts_ppr at prior age
    n_players   INTEGER,    -- sample size
    UNIQUE (position, age)
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


def run():
    conn = get_connection()
    conn.execute(CREATE_NFL_SEASONS)
    conn.execute(CREATE_AGING_CURVE)
    conn.commit()

    # ----------------------------------------------------------------
    # Step 1: Build gsis_id → birth_date map from Sleeper
    # ----------------------------------------------------------------
    print("Fetching Sleeper player registry for birth dates…")
    with urllib.request.urlopen(SLEEPER_PLAYERS_URL) as r:
        sleeper_players = json.loads(r.read())

    gsis_to_birth: dict[str, str] = {}
    for p in sleeper_players.values():
        gid = (p.get("gsis_id") or "").strip()
        bd  = p.get("birth_date")
        if gid and bd:
            gsis_to_birth[gid] = bd
    print(f"  {len(gsis_to_birth)} gsis→birth_date mappings from Sleeper")

    # Also build name→birth_date fallback (for older players not in Sleeper gsis)
    name_to_birth: dict[str, str] = {}
    for p in sleeper_players.values():
        fn = (p.get("full_name") or "").lower()
        bd = p.get("birth_date")
        if fn and bd and p.get("position") in ("QB","RB","WR","TE","K"):
            name_to_birth[fn] = bd

    # ----------------------------------------------------------------
    # Step 2: Ingest all seasons into nfl_player_seasons
    # ----------------------------------------------------------------
    total_rows = 0
    print(f"\nIngesting {len(ALL_YEARS)} seasons…")

    for year in ALL_YEARS:
        url = URL_TMPL.format(year=year)
        try:
            with urllib.request.urlopen(url) as r:
                content = r.read().decode("utf-8")
        except Exception as e:
            print(f"  {year}: FAILED ({e})")
            continue

        reader = csv.DictReader(io.StringIO(content))
        inserted = 0
        for row in reader:
            if row.get("season_type") != "REG":
                continue
            pos = row.get("position", "")
            if pos not in ("QB", "RB", "WR", "TE"):
                continue
            pts = safe_float(row.get("fantasy_points_ppr"))
            if not pts or pts < 10:
                continue  # skip pure garbage-time appearances

            gsis = (row.get("player_id") or "").strip()
            if not gsis:
                continue

            birth = gsis_to_birth.get(gsis)
            if not birth:
                # fallback by display name
                dn = (row.get("player_display_name") or "").lower()
                birth = name_to_birth.get(dn)

            try:
                conn.execute("""
                    INSERT INTO nfl_player_seasons (
                        gsis_id, season_year, display_name, position, team, games,
                        carries, rushing_yards, rushing_tds, rushing_epa,
                        targets, receptions, receiving_yards, receiving_tds, receiving_epa,
                        target_share, air_yards_share, wopr,
                        fantasy_points_ppr, birth_date
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT (gsis_id, season_year) DO UPDATE SET
                        display_name       = excluded.display_name,
                        birth_date         = COALESCE(nfl_player_seasons.birth_date, excluded.birth_date),
                        fantasy_points_ppr = excluded.fantasy_points_ppr,
                        games              = excluded.games,
                        carries            = excluded.carries,
                        rushing_yards      = excluded.rushing_yards,
                        rushing_tds        = excluded.rushing_tds,
                        targets            = excluded.targets,
                        receptions         = excluded.receptions,
                        receiving_yards    = excluded.receiving_yards,
                        target_share       = excluded.target_share,
                        air_yards_share    = excluded.air_yards_share,
                        wopr               = excluded.wopr
                """, (
                    gsis, year,
                    row.get("player_display_name"),
                    pos,
                    row.get("recent_team"),
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
                    pts,
                    birth,
                ))
                inserted += 1
            except Exception as e:
                print(f"  {year} error on {row.get('player_display_name')}: {e}")

        conn.commit()
        total_rows += inserted
        print(f"  {year}: {inserted} rows")

    print(f"\nTotal rows: {total_rows}")

    # ----------------------------------------------------------------
    # Step 3: How many have birth_date? (needed for aging curves)
    # ----------------------------------------------------------------
    has_birth = conn.execute("""
        SELECT position, COUNT(*) as total,
               SUM(CASE WHEN birth_date IS NOT NULL THEN 1 ELSE 0 END) as with_birth
        FROM nfl_player_seasons
        GROUP BY position ORDER BY position
    """).fetchall()
    print("\nBirth date coverage:")
    for r in has_birth:
        pct = r["with_birth"] / r["total"] * 100
        print(f"  {r['position']}: {r['with_birth']}/{r['total']} ({pct:.0f}%)")

    # ----------------------------------------------------------------
    # Step 4: Compute and store aging curves (delta method)
    # ----------------------------------------------------------------
    print("\nComputing aging curves (delta method, games >= 8, pts >= 30)…")

    conn.execute("DELETE FROM aging_curves")

    aging_rows = conn.execute("""
        WITH qualified AS (
            SELECT
                gsis_id,
                season_year,
                position,
                fantasy_points_ppr                                        AS pts,
                CAST(season_year - SUBSTR(birth_date, 1, 4) AS INTEGER)  AS age
            FROM nfl_player_seasons
            WHERE birth_date IS NOT NULL
              AND games >= 8
              AND fantasy_points_ppr >= 30
              AND position IN ('QB','RB','WR','TE')
        ),
        deltas AS (
            SELECT
                a.position,
                a.age,
                AVG(a.pts)            AS avg_pts,
                AVG(b.pts - a.pts)    AS yoy_delta,
                COUNT(*)              AS n
            FROM qualified a
            JOIN qualified b
                ON  a.gsis_id     = b.gsis_id
                AND b.season_year = a.season_year + 1
            GROUP BY a.position, a.age
            HAVING n >= 5
        )
        SELECT
            position, age, avg_pts, yoy_delta,
            ROUND(yoy_delta / avg_pts * 100, 1) AS delta_pct,
            n
        FROM deltas
        ORDER BY position, age
    """).fetchall()

    for r in aging_rows:
        conn.execute("""
            INSERT INTO aging_curves (position, age, avg_pts_ppr, yoy_delta, delta_pct, n_players)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT (position, age) DO UPDATE SET
                avg_pts_ppr = excluded.avg_pts_ppr,
                yoy_delta   = excluded.yoy_delta,
                delta_pct   = excluded.delta_pct,
                n_players   = excluded.n_players
        """, (r["position"], r["age"], r["avg_pts"], r["yoy_delta"], r["delta_pct"], r["n"]))

    conn.commit()
    print(f"  Stored {len(aging_rows)} aging curve data points")

    # Print the curves
    current_pos = None
    for r in aging_rows:
        if r["position"] != current_pos:
            print(f"\n  {r['position']:>2}  age  avg_pts  yoy_Δ    Δ%     n")
            current_pos = r["position"]
        delta_str = f"+{r['yoy_delta']:.1f}" if r["yoy_delta"] >= 0 else f"{r['yoy_delta']:.1f}"
        pct_str   = f"+{r['delta_pct']:.1f}%" if r["delta_pct"] >= 0 else f"{r['delta_pct']:.1f}%"
        print(f"       {r['age']:>3}  {r['avg_pts']:>7.1f}  {delta_str:>7}  {pct_str:>7}  ({r['n']})")

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    run()
