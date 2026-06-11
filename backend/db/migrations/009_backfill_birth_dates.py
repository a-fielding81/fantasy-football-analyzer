"""
Migration 009 — Backfill birth dates in nfl_player_seasons from nflverse players registry.

nflverse maintains a comprehensive players.csv with birth dates for 25,000+
players including all historical players back to the 1990s. Sleeper only has
current-era players, leaving 1999-2014 seasons mostly without birth dates.

This migration:
1. Downloads nflverse players.csv
2. Builds gsis_id → birth_date map (24,904 entries)
3. Updates nfl_player_seasons.birth_date for all rows where it's NULL
4. Also backfills players.birth_date in our main players table where missing
5. Recomputes aging_curves with the full dataset
"""

import sys
import csv
import io
import urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from db.database import get_connection

NFLVERSE_PLAYERS_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/players/players.csv"
)


def run():
    conn = get_connection()

    # ── Step 1: download nflverse players registry ──
    print("Downloading nflverse players registry…")
    with urllib.request.urlopen(NFLVERSE_PLAYERS_URL) as r:
        content = r.read().decode("utf-8")

    reader = csv.DictReader(io.StringIO(content))
    gsis_to_birth: dict[str, str] = {}
    name_to_birth: dict[str, str] = {}  # "first last" lowered → birth_date

    for row in reader:
        gsis = (row.get("gsis_id") or "").strip()
        bd   = (row.get("birth_date") or "").strip()
        if not bd:
            continue
        if gsis:
            gsis_to_birth[gsis] = bd
        # Also index by display_name for fallback
        dn = (row.get("display_name") or "").strip().lower()
        if dn:
            name_to_birth[dn] = bd

    print(f"  {len(gsis_to_birth)} gsis→birth_date entries")
    print(f"  {len(name_to_birth)} name→birth_date entries")

    # ── Step 2: update nfl_player_seasons ──
    print("\nUpdating nfl_player_seasons.birth_date…")

    missing = conn.execute(
        "SELECT DISTINCT gsis_id, display_name FROM nfl_player_seasons WHERE birth_date IS NULL"
    ).fetchall()
    print(f"  {len(missing)} distinct players with missing birth_date")

    updated_gsis = updated_name = still_missing = 0
    for row in missing:
        gsis = (row["gsis_id"] or "").strip()
        bd = gsis_to_birth.get(gsis)
        if bd:
            conn.execute(
                "UPDATE nfl_player_seasons SET birth_date = ? WHERE gsis_id = ?",
                (bd, gsis)
            )
            updated_gsis += 1
        else:
            dn = (row["display_name"] or "").strip().lower()
            bd = name_to_birth.get(dn)
            if bd:
                conn.execute(
                    "UPDATE nfl_player_seasons SET birth_date = ? WHERE gsis_id = ?",
                    (bd, gsis)
                )
                updated_name += 1
            else:
                still_missing += 1

    conn.commit()
    print(f"  Updated via gsis: {updated_gsis}")
    print(f"  Updated via name fallback: {updated_name}")
    print(f"  Still missing: {still_missing}")

    # ── Step 3: also backfill our main players table ──
    print("\nBackfilling players.birth_date…")
    db_missing = conn.execute(
        "SELECT id, gsis_id, full_name FROM players WHERE birth_date IS NULL AND gsis_id IS NOT NULL"
    ).fetchall()
    p_updated = 0
    for row in db_missing:
        gsis = (row["gsis_id"] or "").strip()
        bd = gsis_to_birth.get(gsis)
        if bd:
            conn.execute("UPDATE players SET birth_date = ? WHERE id = ?", (bd, row["id"]))
            p_updated += 1
    conn.commit()
    print(f"  Updated {p_updated} player rows")

    # ── Step 4: show new coverage ──
    print("\nUpdated birth_date coverage by season:")
    rows = conn.execute("""
        SELECT season_year,
            COUNT(*) as total,
            SUM(CASE WHEN birth_date IS NOT NULL THEN 1 ELSE 0 END) as with_birth
        FROM nfl_player_seasons
        GROUP BY season_year ORDER BY season_year
    """).fetchall()
    for r in rows:
        pct = r["with_birth"] / r["total"] * 100
        bar = "█" * int(pct / 5)
        print(f"  {r['season_year']}: {r['with_birth']:>4}/{r['total']:>4} ({pct:>4.0f}%)  {bar}")

    # ── Step 5: recompute aging curves with full dataset ──
    print("\nRecomputing aging curves with full dataset…")
    conn.execute("DELETE FROM aging_curves")

    aging_rows = conn.execute("""
        WITH qualified AS (
            SELECT
                gsis_id,
                season_year,
                position,
                fantasy_points_ppr                                       AS pts,
                CAST(season_year - SUBSTR(birth_date, 1, 4) AS INTEGER) AS age
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
        SELECT position, age, avg_pts, yoy_delta,
               ROUND(yoy_delta / avg_pts * 100, 1) AS delta_pct, n
        FROM deltas
        ORDER BY position, age
    """).fetchall()

    for r in aging_rows:
        conn.execute("""
            INSERT INTO aging_curves (position, age, avg_pts_ppr, yoy_delta, delta_pct, n_players)
            VALUES (?,?,?,?,?,?)
        """, (r["position"], r["age"], r["avg_pts"], r["yoy_delta"], r["delta_pct"], r["n"]))
    conn.commit()

    print(f"  {len(aging_rows)} aging curve data points")
    print()
    current_pos = None
    for r in aging_rows:
        if r["position"] != current_pos:
            print(f"  {r['position']}  age  avg_pts  yoy_Δ    Δ%    n")
            current_pos = r["position"]
        ds = f"+{r['yoy_delta']:.1f}" if r["yoy_delta"] >= 0 else f"{r['yoy_delta']:.1f}"
        ps = f"+{r['delta_pct']:.1f}%" if r["delta_pct"] >= 0 else f"{r['delta_pct']:.1f}%"
        print(f"       {r['age']:>3}  {r['avg_pts']:>7.1f}  {ds:>7}  {ps:>7}  ({r['n']})")

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    run()
