"""
Migration 013 — Ingest 2025 nflverse season data.

The original migrations (007, 010) were hardcoded to stop at 2024 using the
old nflverse URL pattern (player_stats/player_stats_season_{year}.csv).
nflverse renamed their releases in 2025:

  Old:  player_stats/player_stats_season_{year}.csv   (≤2024)
  New:  stats_player/stats_player_reg_{year}.csv       (2025+)

  Old:  player_stats/stats_team_reg_{year}.csv         (≤2024)
  New:  stats_team/stats_team_reg_{year}.csv            (2025+)

This migration:
  1. Downloads stats_player_reg_2025.csv → inserts into nfl_player_seasons
  2. Downloads stats_team_reg_2025.csv → updates team_season_scheme pass rate
  3. Propagates 2025 coaching + scheme features from team_season_scheme to
     the new nfl_player_seasons rows (same logic as migrations 011/012).

Run from backend/:
    python db/migrations/013_ingest_2025_season.py
"""

import sys, csv, io, urllib.request
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from db.database import get_connection

PLAYER_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "stats_player/stats_player_reg_2025.csv"
)
TEAM_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "stats_team/stats_team_reg_2025.csv"
)

YEAR = 2025


def fetch_csv(url: str, label: str) -> list:
    print(f"  Downloading {label}…", end=" ", flush=True)
    with urllib.request.urlopen(url, timeout=60) as r:
        raw = r.read().decode("utf-8", errors="replace")
    rows = list(csv.DictReader(io.StringIO(raw)))
    print(f"{len(rows)} rows")
    return rows


def _float(val) -> Optional[float]:
    try:
        return float(val) if val not in (None, "", "NA", "NaN") else None
    except (ValueError, TypeError):
        return None


def _int(val) -> Optional[int]:
    f = _float(val)
    return int(f) if f is not None else None


# ── Step 1: player season stats ─────────────────────────────────────────────

def ingest_player_stats(conn, rows: list) -> int:
    inserted = skipped = 0

    # Pre-load birth dates from players table keyed by gsis_id
    birth_dates = {r["gsis_id"]: r["birth_date"]
                   for r in conn.execute("SELECT gsis_id, birth_date FROM players WHERE gsis_id IS NOT NULL")}

    for row in rows:
        gsis = (row.get("player_id") or "").strip()
        if not gsis:
            continue

        season_type = (row.get("season_type") or "").upper()
        if season_type not in ("REG", ""):
            continue                        # skip postseason rows

        pos = (row.get("position") or "").strip()
        if pos not in ("QB", "RB", "WR", "TE"):
            continue                        # only skill positions

        team = (row.get("recent_team") or "").strip()
        games = _int(row.get("games"))
        if not games or games < 1:
            skipped += 1
            continue

        bd = birth_dates.get(gsis)

        conn.execute("""
            INSERT INTO nfl_player_seasons (
                gsis_id, season_year, display_name, position, team, games,
                carries, rushing_yards, rushing_tds, rushing_epa,
                targets, receptions, receiving_yards, receiving_tds, receiving_epa,
                target_share, air_yards_share, wopr, fantasy_points_ppr, birth_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (gsis_id, season_year) DO UPDATE SET
                display_name       = excluded.display_name,
                position           = excluded.position,
                team               = excluded.team,
                games              = excluded.games,
                carries            = excluded.carries,
                rushing_yards      = excluded.rushing_yards,
                rushing_tds        = excluded.rushing_tds,
                rushing_epa        = excluded.rushing_epa,
                targets            = excluded.targets,
                receptions         = excluded.receptions,
                receiving_yards    = excluded.receiving_yards,
                receiving_tds      = excluded.receiving_tds,
                receiving_epa      = excluded.receiving_epa,
                target_share       = excluded.target_share,
                air_yards_share    = excluded.air_yards_share,
                wopr               = excluded.wopr,
                fantasy_points_ppr = excluded.fantasy_points_ppr,
                birth_date         = excluded.birth_date
        """, (
            gsis, YEAR,
            row.get("player_display_name") or row.get("player_name") or "",
            pos, team, games,
            _int(row.get("carries")),
            _float(row.get("rushing_yards")),
            _int(row.get("rushing_tds")),
            _float(row.get("rushing_epa")),
            _int(row.get("targets")),
            _int(row.get("receptions")),
            _float(row.get("receiving_yards")),
            _int(row.get("receiving_tds")),
            _float(row.get("receiving_epa")),
            _float(row.get("target_share")),
            _float(row.get("air_yards_share")),
            _float(row.get("wopr")),
            _float(row.get("fantasy_points_ppr")),
            bd,
        ))
        inserted += 1

    conn.commit()
    return inserted


# ── Step 2: team pass rate ───────────────────────────────────────────────────

def update_team_pass_rate(conn, rows: list) -> int:
    updated = 0
    for row in rows:
        season_type = (row.get("season_type") or "").upper()
        if season_type not in ("REG", ""):
            continue

        team = (row.get("team") or "").strip()
        pa   = _int(row.get("attempts")) or 0
        ra   = _int(row.get("carries")) or 0
        total = pa + ra
        if total < 100 or not team:
            continue

        pass_rate = pa / total

        # Upsert into team_season_scheme
        conn.execute("""
            INSERT INTO team_season_scheme (season_year, team, pass_rate)
            VALUES (?, ?, ?)
            ON CONFLICT (season_year, team) DO UPDATE SET
                pass_rate = excluded.pass_rate
        """, (YEAR, team, round(pass_rate, 4)))
        updated += 1

    conn.commit()
    return updated


# ── Step 3: propagate scheme features to nfl_player_seasons ─────────────────

def propagate_scheme_features(conn) -> int:
    """Copy team_season_scheme columns into nfl_player_seasons for 2025."""
    result = conn.execute("""
        UPDATE nfl_player_seasons
        SET
            team_pass_rate         = (SELECT tss.pass_rate
                                      FROM team_season_scheme tss
                                      WHERE tss.season_year = nfl_player_seasons.season_year
                                        AND tss.team        = nfl_player_seasons.team),
            team_hc_midseason_change = (SELECT tss.hc_midseason_change
                                        FROM team_season_scheme tss
                                        WHERE tss.season_year = nfl_player_seasons.season_year
                                          AND tss.team        = nfl_player_seasons.team),
            team_oc_midseason_change = (SELECT tss.oc_midseason_change
                                        FROM team_season_scheme tss
                                        WHERE tss.season_year = nfl_player_seasons.season_year
                                          AND tss.team        = nfl_player_seasons.team),
            team_11_rate           = (SELECT tss.rate_11
                                      FROM team_season_scheme tss
                                      WHERE tss.season_year = nfl_player_seasons.season_year
                                        AND tss.team        = nfl_player_seasons.team),
            team_shotgun_rate      = (SELECT tss.rate_shotgun
                                      FROM team_season_scheme tss
                                      WHERE tss.season_year = nfl_player_seasons.season_year
                                        AND tss.team        = nfl_player_seasons.team),
            team_new_oc            = (SELECT tss.new_oc
                                      FROM team_season_scheme tss
                                      WHERE tss.season_year = nfl_player_seasons.season_year
                                        AND tss.team        = nfl_player_seasons.team),
            team_hc_tenure         = (SELECT tss.hc_tenure
                                      FROM team_season_scheme tss
                                      WHERE tss.season_year = nfl_player_seasons.season_year
                                        AND tss.team        = nfl_player_seasons.team),
            coaching_tree          = (SELECT tss.coaching_tree
                                      FROM team_season_scheme tss
                                      WHERE tss.season_year = nfl_player_seasons.season_year
                                        AND tss.team        = nfl_player_seasons.team)
        WHERE season_year = ?
    """, (YEAR,))
    conn.commit()
    return result.rowcount


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    conn = get_connection()

    print(f"\n=== Migration 013: Ingest {YEAR} nflverse season data ===\n")

    print("Step 1: Player season stats")
    player_rows = fetch_csv(PLAYER_URL, f"stats_player_reg_{YEAR}.csv")
    n = ingest_player_stats(conn, player_rows)
    print(f"  → {n} rows inserted/updated in nfl_player_seasons")

    print("\nStep 2: Team pass rate")
    team_rows = fetch_csv(TEAM_URL, f"stats_team_reg_{YEAR}.csv")
    n = update_team_pass_rate(conn, team_rows)
    print(f"  → {n} teams updated in team_season_scheme")

    print("\nStep 3: Propagate scheme features to nfl_player_seasons")
    n = propagate_scheme_features(conn)
    print(f"  → {n} rows updated")

    # Verify key players
    print(f"\nVerification — sample {YEAR} rows:")
    rows = conn.execute("""
        SELECT display_name, position, team, games, fantasy_points_ppr,
               team_pass_rate, coaching_tree
        FROM nfl_player_seasons
        WHERE season_year = ? AND position IN ('QB','RB','WR','TE')
          AND fantasy_points_ppr >= 150
        ORDER BY fantasy_points_ppr DESC
        LIMIT 10
    """, (YEAR,)).fetchall()
    for r in rows:
        print(f"  {r['display_name']:<22} {r['position']} {r['team']}  "
              f"{r['games']}g  {r['fantasy_points_ppr']:.1f}ppr  "
              f"pass_rate={r['team_pass_rate'] or '?'}  tree={r['coaching_tree'] or '?'}")

    conn.close()
    print("\nDone.")
