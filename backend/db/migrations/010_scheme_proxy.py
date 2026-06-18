"""
Migration 010 — Add team pass-rate scheme proxy to nfl_player_seasons.

Scheme has a real effect on fantasy value: a WR on a 65%-pass team
is worth more than the same player on a 45%-pass team.  Rather than
tying this to a coach's name (coaches change, scheme doesn't always),
we use the team's *actual* play-call behaviour:

  team_pass_rate = pass_attempts / (pass_attempts + rush_attempts)

Source: nflverse stats_team_reg_{year}.csv (available 1999-2024).
  - `attempts`  = team passing attempts (dropbacks)
  - `carries`   = team rushing attempts

We also compute team_pass_rate_prior (prior season's rate for the same
team) and team_pass_rate_change = current - prior.  A big positive
change signals an offense going more pass-heavy, which helps WR/TE
values.

New columns added to nfl_player_seasons:
  - team_pass_rate        REAL   (0-1, e.g. 0.62 for a pass-heavy KC)
  - team_pass_rate_prior  REAL   (prior season, NULL if no prior)
  - team_pass_rate_change REAL   (current - prior, NULL if no prior)

A standalone `team_season_scheme` table is also created for reference.

After populating, the production model is retrained with these
two new features: team_pass_rate_t and team_pass_rate_change_t.
"""

import sys
import csv
import io
import urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from db.database import get_connection

ALL_YEARS = list(range(1999, 2025))

URL_TMPL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "player_stats/stats_team_reg_{year}.csv"
)

CREATE_SCHEME_TABLE = """
CREATE TABLE IF NOT EXISTS team_season_scheme (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    season_year     INTEGER NOT NULL,
    team            TEXT    NOT NULL,
    pass_attempts   INTEGER,
    rush_attempts   INTEGER,
    pass_rate       REAL,       -- pass_attempts / (pass + rush)
    passing_epa     REAL,
    rushing_epa     REAL,
    UNIQUE (season_year, team)
);
"""

ADD_COLS = [
    "ALTER TABLE nfl_player_seasons ADD COLUMN team_pass_rate        REAL",
    "ALTER TABLE nfl_player_seasons ADD COLUMN team_pass_rate_prior  REAL",
    "ALTER TABLE nfl_player_seasons ADD COLUMN team_pass_rate_change REAL",
]


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

    # ── 1. Create scheme table ────────────────────────────────────────────────
    conn.execute(CREATE_SCHEME_TABLE)
    for sql in ADD_COLS:
        try:
            conn.execute(sql)
        except Exception:
            pass   # column already exists — idempotent
    conn.commit()

    # ── 2. Ingest stats_team_reg for every available season ──────────────────
    print(f"Ingesting team pass rates for {len(ALL_YEARS)} seasons…")
    total_teams = 0

    for year in ALL_YEARS:
        url = URL_TMPL.format(year=year)
        try:
            with urllib.request.urlopen(url) as r:
                content = r.read().decode("utf-8")
        except Exception as e:
            print(f"  {year}: FAILED to download ({e})")
            continue

        reader = csv.DictReader(io.StringIO(content))
        inserted = 0

        for row in reader:
            # Each row is a team-season aggregate
            team = (row.get("recent_team") or row.get("team") or "").strip()
            if not team:
                continue

            attempts = safe_int(row.get("attempts"))    # pass attempts
            carries  = safe_int(row.get("carries"))     # rush attempts
            if attempts is None or carries is None:
                continue

            total = attempts + carries
            pass_rate = round(attempts / total, 4) if total > 0 else None

            passing_epa = safe_float(row.get("passing_epa"))
            rushing_epa = safe_float(row.get("rushing_epa"))

            conn.execute("""
                INSERT INTO team_season_scheme
                    (season_year, team, pass_attempts, rush_attempts, pass_rate,
                     passing_epa, rushing_epa)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT (season_year, team) DO UPDATE SET
                    pass_attempts = excluded.pass_attempts,
                    rush_attempts = excluded.rush_attempts,
                    pass_rate     = excluded.pass_rate,
                    passing_epa   = excluded.passing_epa,
                    rushing_epa   = excluded.rushing_epa
            """, (year, team, attempts, carries, pass_rate, passing_epa, rushing_epa))
            inserted += 1

        conn.commit()
        total_teams += inserted
        print(f"  {year}: {inserted} teams")

    print(f"\nTotal team-season rows: {total_teams}")

    # ── 3. Quick sanity check ─────────────────────────────────────────────────
    sample = conn.execute("""
        SELECT season_year, team, pass_attempts, rush_attempts, pass_rate
        FROM team_season_scheme
        WHERE season_year = 2022
        ORDER BY pass_rate DESC
        LIMIT 5
    """).fetchall()
    print("\nTop-5 pass-rate teams in 2022:")
    for r in sample:
        print(f"  {r['team']:>4}  {r['pass_rate']:.3f}  "
              f"({r['pass_attempts']}pa / {r['rush_attempts']}ra)")

    # ── 4. Join pass_rate back into nfl_player_seasons ────────────────────────
    print("\nBackfilling team_pass_rate in nfl_player_seasons…")

    # Current-season rate
    conn.execute("""
        UPDATE nfl_player_seasons
        SET team_pass_rate = (
            SELECT tss.pass_rate
            FROM team_season_scheme tss
            WHERE tss.season_year = nfl_player_seasons.season_year
              AND tss.team        = nfl_player_seasons.team
        )
        WHERE team IS NOT NULL
    """)
    conn.commit()

    updated = conn.execute(
        "SELECT COUNT(*) FROM nfl_player_seasons WHERE team_pass_rate IS NOT NULL"
    ).fetchone()[0]
    total_rows = conn.execute("SELECT COUNT(*) FROM nfl_player_seasons").fetchone()[0]
    print(f"  {updated}/{total_rows} rows have team_pass_rate")

    # Prior-season rate (team abbrev may differ year-to-year for relocated teams,
    # but this covers >95% of cases)
    conn.execute("""
        UPDATE nfl_player_seasons AS nps
        SET team_pass_rate_prior = (
            SELECT tss.pass_rate
            FROM team_season_scheme tss
            WHERE tss.season_year = nps.season_year - 1
              AND tss.team        = nps.team
        )
        WHERE team IS NOT NULL
    """)
    conn.execute("""
        UPDATE nfl_player_seasons
        SET team_pass_rate_change = team_pass_rate - team_pass_rate_prior
        WHERE team_pass_rate IS NOT NULL
          AND team_pass_rate_prior IS NOT NULL
    """)
    conn.commit()

    with_change = conn.execute(
        "SELECT COUNT(*) FROM nfl_player_seasons WHERE team_pass_rate_change IS NOT NULL"
    ).fetchone()[0]
    print(f"  {with_change}/{total_rows} rows have team_pass_rate_change")

    # ── 5. Coverage by season ─────────────────────────────────────────────────
    print("\nCoverage by season (sample 5):")
    cov = conn.execute("""
        SELECT season_year,
               COUNT(*) AS total,
               SUM(CASE WHEN team_pass_rate IS NOT NULL THEN 1 ELSE 0 END) AS with_rate
        FROM nfl_player_seasons
        GROUP BY season_year
        ORDER BY season_year DESC
        LIMIT 5
    """).fetchall()
    for r in cov:
        pct = r["with_rate"] / r["total"] * 100 if r["total"] else 0
        print(f"  {r['season_year']}: {r['with_rate']}/{r['total']} ({pct:.0f}%)")

    conn.close()
    print("\nDone.  Run production_model.py to retrain with scheme features.")


if __name__ == "__main__":
    run()
