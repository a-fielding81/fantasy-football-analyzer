"""
Migration 012 — Offensive personnel grouping & formation rates per team-season.

Source: nflverse pbp_participation_{year}.csv (2016–2025)
        nflverse ftn_charting_{year}.csv (2022–2025)

Personnel groupings (from offense_personnel column, e.g. "1 RB, 1 TE, 3 WR"):
  11 personnel  = 1 RB, 1 TE, 3 WR   (most common in modern pass-heavy offenses)
  12 personnel  = 1 RB, 2 TE, 2 WR   (balanced/run-game, SF-style)
  21 personnel  = 2 RB, 1 TE, 2 WR   (power run)
  10 personnel  = 1 RB, 0 TE, 4 WR   (spread/empty-style)
  13 personnel  = 1 RB, 3 TE, 1 WR   (heavy run / red zone)
  22 personnel  = 2 RB, 2 TE, 1 WR   (jumbo / short yardage)

Formation rates (from offense_formation):
  shotgun_rate, pistol_rate, under_center_rate

Coverage tendencies (from defense_coverage_type for opponents):
  (stored for reference, used as context feature)

FTN-derived (2022+ only):
  play_action_rate, motion_rate, rpo_rate, screen_rate, no_huddle_rate

All aggregated as rates (fraction of team's plays that season, offense plays only).

New columns added to team_season_scheme:
  rate_11, rate_12, rate_21, rate_10, rate_13, rate_22
  rate_shotgun, rate_pistol, rate_under_center
  rate_play_action, rate_motion, rate_rpo     (NULL for pre-2022)

Also added to nfl_player_seasons:
  team_11_rate   — key signal for WR/TE value
  team_shotgun_rate (replaces pass rate as a within-season scheme proxy)
  team_new_oc    — disruption flag
  team_hc_tenure
  coaching_tree
"""

import sys
import csv
import io
import urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from db.database import get_connection

PARTICIPATION_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "pbp_participation/pbp_participation_{year}.csv"
)
FTN_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "ftn_charting/ftn_charting_{year}.csv"
)

PARTICIPATION_YEARS = list(range(2016, 2026))
FTN_YEARS          = list(range(2022, 2026))

# Personnel string → grouping label
PERSONNEL_LABELS = {
    "1 RB, 1 TE, 3 WR": "11",
    "1 RB, 2 TE, 2 WR": "12",
    "2 RB, 1 TE, 2 WR": "21",
    "1 RB, 0 TE, 4 WR": "10",
    "0 RB, 1 TE, 4 WR": "10",
    "1 RB, 3 TE, 1 WR": "13",
    "2 RB, 2 TE, 1 WR": "22",
    "2 RB, 0 TE, 3 WR": "20",
}

ADD_SCHEME_COLS = [
    "ALTER TABLE team_season_scheme ADD COLUMN rate_11          REAL",
    "ALTER TABLE team_season_scheme ADD COLUMN rate_12          REAL",
    "ALTER TABLE team_season_scheme ADD COLUMN rate_21          REAL",
    "ALTER TABLE team_season_scheme ADD COLUMN rate_10          REAL",
    "ALTER TABLE team_season_scheme ADD COLUMN rate_13          REAL",
    "ALTER TABLE team_season_scheme ADD COLUMN rate_22          REAL",
    "ALTER TABLE team_season_scheme ADD COLUMN rate_shotgun     REAL",
    "ALTER TABLE team_season_scheme ADD COLUMN rate_pistol      REAL",
    "ALTER TABLE team_season_scheme ADD COLUMN rate_under_center REAL",
    "ALTER TABLE team_season_scheme ADD COLUMN rate_play_action REAL",
    "ALTER TABLE team_season_scheme ADD COLUMN rate_motion      REAL",
    "ALTER TABLE team_season_scheme ADD COLUMN rate_rpo         REAL",
    "ALTER TABLE team_season_scheme ADD COLUMN rate_no_huddle   REAL",
    "ALTER TABLE team_season_scheme ADD COLUMN n_offense_plays  INTEGER",
]

ADD_PLAYER_COLS = [
    "ALTER TABLE nfl_player_seasons ADD COLUMN team_11_rate      REAL",
    "ALTER TABLE nfl_player_seasons ADD COLUMN team_shotgun_rate REAL",
    "ALTER TABLE nfl_player_seasons ADD COLUMN team_new_oc       INTEGER",
    "ALTER TABLE nfl_player_seasons ADD COLUMN team_hc_tenure    INTEGER",
    "ALTER TABLE nfl_player_seasons ADD COLUMN coaching_tree     TEXT",
]


def safe_float(v):
    try:
        return float(v) if v not in (None, "", "NA", "nan") else None
    except (TypeError, ValueError):
        return None


def bool_val(v):
    return 1 if str(v).strip().upper() == "TRUE" else 0


# ── Step 1: aggregate participation data ─────────────────────────────────

def aggregate_participation(year: int):
    """
    Return dict: team → {rate_11, rate_12, ..., rate_shotgun, n_plays}
    Only offensive plays with known possession team.
    """
    url = PARTICIPATION_URL.format(year=year)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "FFAnalyzer/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            content = r.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"  {year} participation: FAILED ({e})")
        return {}

    reader = csv.DictReader(io.StringIO(content))

    # Accumulators: team → counter dict
    from collections import defaultdict
    counts = defaultdict(lambda: defaultdict(int))

    for row in reader:
        team = (row.get("possession_team") or "").strip()
        if not team:
            continue

        personnel = (row.get("offense_personnel") or "").strip()
        formation = (row.get("offense_formation") or "").strip().upper()

        counts[team]["total"] += 1

        # Personnel grouping
        label = PERSONNEL_LABELS.get(personnel)
        if label:
            counts[team][f"p_{label}"] += 1

        # Formation
        if formation == "SHOTGUN":
            counts[team]["f_shotgun"] += 1
        elif formation == "PISTOL":
            counts[team]["f_pistol"] += 1
        elif formation in ("UNDER_CENTER", "I_FORM", "SINGLEBACK", "JUMBO"):
            counts[team]["f_under_center"] += 1

    # Convert to rates
    result = {}
    for team, c in counts.items():
        n = c["total"]
        if n < 100:
            continue
        result[team] = {
            "n_plays":          n,
            "rate_11":          round(c["p_11"] / n, 4),
            "rate_12":          round(c["p_12"] / n, 4),
            "rate_21":          round(c["p_21"] / n, 4),
            "rate_10":          round(c["p_10"] / n, 4),
            "rate_13":          round(c["p_13"] / n, 4),
            "rate_22":          round(c["p_22"] / n, 4),
            "rate_shotgun":     round(c["f_shotgun"] / n, 4),
            "rate_pistol":      round(c["f_pistol"] / n, 4),
            "rate_under_center":round(c["f_under_center"] / n, 4),
        }

    return result


# ── Step 2: aggregate FTN charting ────────────────────────────────────────

def aggregate_ftn(year: int):
    """Return dict: (team derived from game_id) → play_action_rate, etc.
    FTN doesn't have team directly; we join on nflverse_game_id × play_id
    from the participation data. Simpler: we infer posteam from game play data.
    Since FTN doesn't store team, we use nflverse_game_id play counts to
    approximate by joining back through the participation data.

    Approach: download both participation and FTN for the same year, join on
    (nflverse_game_id, nflverse_play_id) to get possession_team, then aggregate.
    """
    # First get a game_id+play_id → team map from participation
    part_url = PARTICIPATION_URL.format(year=year)
    ftn_url  = FTN_URL.format(year=year)

    try:
        req = urllib.request.Request(part_url, headers={"User-Agent": "FFAnalyzer/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            part_content = r.read().decode("utf-8", errors="ignore")
        req2 = urllib.request.Request(ftn_url, headers={"User-Agent": "FFAnalyzer/1.0"})
        with urllib.request.urlopen(req2, timeout=60) as r:
            ftn_content = r.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"  {year} FTN: FAILED ({e})")
        return {}

    # Build join key: (game_id, play_id) → possession_team
    play_team = {}
    for row in csv.DictReader(io.StringIO(part_content)):
        gid = row.get("nflverse_game_id", "").strip()
        pid = row.get("play_id", "").strip()
        team = row.get("possession_team", "").strip()
        if gid and pid and team:
            play_team[(gid, pid)] = team

    from collections import defaultdict
    counts = defaultdict(lambda: defaultdict(int))

    for row in csv.DictReader(io.StringIO(ftn_content)):
        gid = row.get("nflverse_game_id", "").strip()
        pid = row.get("nflverse_play_id", "").strip()
        team = play_team.get((gid, pid))
        if not team:
            continue

        counts[team]["total"] += 1
        counts[team]["play_action"] += bool_val(row.get("is_play_action"))
        counts[team]["motion"]      += bool_val(row.get("is_motion"))
        counts[team]["rpo"]         += bool_val(row.get("is_rpo"))
        counts[team]["no_huddle"]   += bool_val(row.get("is_no_huddle"))
        counts[team]["screen"]      += bool_val(row.get("is_screen_pass"))

    result = {}
    for team, c in counts.items():
        n = c["total"]
        if n < 50:
            continue
        result[team] = {
            "rate_play_action": round(c["play_action"] / n, 4),
            "rate_motion":      round(c["motion"] / n, 4),
            "rate_rpo":         round(c["rpo"] / n, 4),
            "rate_no_huddle":   round(c["no_huddle"] / n, 4),
        }

    return result


# ── main ─────────────────────────────────────────────────────────────────

def run():
    conn = get_connection()

    for sql in ADD_SCHEME_COLS + ADD_PLAYER_COLS:
        try:
            conn.execute(sql)
        except Exception:
            pass
    conn.commit()

    # ── Participation data ─────────────────────────────────────────────────
    print(f"Processing participation data ({PARTICIPATION_YEARS[0]}–{PARTICIPATION_YEARS[-1]})…")
    total_teams = 0

    for year in PARTICIPATION_YEARS:
        data = aggregate_participation(year)
        if not data:
            continue

        for team, stats in data.items():
            conn.execute("""
                UPDATE team_season_scheme
                SET n_offense_plays   = ?,
                    rate_11           = ?,
                    rate_12           = ?,
                    rate_21           = ?,
                    rate_10           = ?,
                    rate_13           = ?,
                    rate_22           = ?,
                    rate_shotgun      = ?,
                    rate_pistol       = ?,
                    rate_under_center = ?
                WHERE season_year = ? AND team = ?
            """, (
                stats["n_plays"],
                stats["rate_11"], stats["rate_12"], stats["rate_21"],
                stats["rate_10"], stats["rate_13"], stats["rate_22"],
                stats["rate_shotgun"], stats["rate_pistol"], stats["rate_under_center"],
                year, team,
            ))

        conn.commit()
        teams_updated = len(data)
        total_teams += teams_updated
        # Print a 2022 sample inline
        if year == 2022:
            sample = sorted(data.items(), key=lambda x: x[1]["rate_11"], reverse=True)[:5]
            top5 = ", ".join(f"{t}={v['rate_11']:.2f}" for t, v in sample)
            print(f"  {year}: {teams_updated} teams  |  top 11-pct: {top5}")
        else:
            print(f"  {year}: {teams_updated} teams")

    # ── FTN charting data ──────────────────────────────────────────────────
    print(f"\nProcessing FTN charting data ({FTN_YEARS[0]}–{FTN_YEARS[-1]})…")

    for year in FTN_YEARS:
        ftn = aggregate_ftn(year)
        if not ftn:
            continue

        for team, stats in ftn.items():
            conn.execute("""
                UPDATE team_season_scheme
                SET rate_play_action = ?,
                    rate_motion      = ?,
                    rate_rpo         = ?,
                    rate_no_huddle   = ?
                WHERE season_year = ? AND team = ?
            """, (
                stats["rate_play_action"], stats["rate_motion"],
                stats["rate_rpo"], stats["rate_no_huddle"],
                year, team,
            ))

        conn.commit()
        sample = sorted(ftn.items(), key=lambda x: x[1]["rate_play_action"], reverse=True)[:3]
        top3 = ", ".join(f"{t}={v['rate_play_action']:.2f}" for t, v in sample)
        print(f"  {year}: {len(ftn)} teams  |  top PA rate: {top3}")

    # ── Backfill nfl_player_seasons ────────────────────────────────────────
    print("\nBackfilling nfl_player_seasons with scheme features…")

    conn.execute("""
        UPDATE nfl_player_seasons AS nps
        SET team_11_rate = (
            SELECT tss.rate_11 FROM team_season_scheme tss
            WHERE tss.season_year = nps.season_year AND tss.team = nps.team
        ),
        team_shotgun_rate = (
            SELECT tss.rate_shotgun FROM team_season_scheme tss
            WHERE tss.season_year = nps.season_year AND tss.team = nps.team
        ),
        team_new_oc = (
            SELECT tss.new_oc FROM team_season_scheme tss
            WHERE tss.season_year = nps.season_year AND tss.team = nps.team
        ),
        team_hc_tenure = (
            SELECT tss.hc_tenure FROM team_season_scheme tss
            WHERE tss.season_year = nps.season_year AND tss.team = nps.team
        ),
        coaching_tree = (
            SELECT tss.coaching_tree FROM team_season_scheme tss
            WHERE tss.season_year = nps.season_year AND tss.team = nps.team
        )
        WHERE nps.team IS NOT NULL
    """)
    conn.commit()

    with11 = conn.execute(
        "SELECT COUNT(*) FROM nfl_player_seasons WHERE team_11_rate IS NOT NULL"
    ).fetchone()[0]
    total  = conn.execute("SELECT COUNT(*) FROM nfl_player_seasons").fetchone()[0]
    print(f"  team_11_rate populated: {with11}/{total} rows")

    # ── Sanity check ────────────────────────────────────────────────────────
    print("\nTop-5 11-personnel teams in 2022:")
    rows = conn.execute("""
        SELECT team, rate_11, rate_12, rate_shotgun, hc_name, oc_name, coaching_tree
        FROM team_season_scheme
        WHERE season_year = 2022 AND rate_11 IS NOT NULL
        ORDER BY rate_11 DESC LIMIT 5
    """).fetchall()
    for r in rows:
        print(f"  {r['team']:>4}  11%={r['rate_11']:.2f}  12%={r['rate_12'] or 0:.2f}  "
              f"SG%={r['rate_shotgun'] or 0:.2f}  "
              f"HC={r['hc_name'] or '?':20}  OC={r['oc_name'] or '?':20}  "
              f"tree={r['coaching_tree']}")

    print("\nBottom-5 (run-heavy teams) 2022:")
    rows = conn.execute("""
        SELECT team, rate_11, rate_12, rate_21, hc_name, coaching_tree
        FROM team_season_scheme
        WHERE season_year = 2022 AND rate_11 IS NOT NULL
        ORDER BY rate_11 ASC LIMIT 5
    """).fetchall()
    for r in rows:
        print(f"  {r['team']:>4}  11%={r['rate_11']:.2f}  12%={r['rate_12'] or 0:.2f}  "
              f"21%={r['rate_21'] or 0:.2f}  HC={r['hc_name'] or '?':20}  tree={r['coaching_tree']}")

    conn.close()
    print("\nDone. Run production_model.py to retrain with all new features.")


if __name__ == "__main__":
    run()
