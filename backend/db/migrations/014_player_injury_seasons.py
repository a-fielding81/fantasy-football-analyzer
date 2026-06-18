"""
Migration 014 — Player season injury aggregates.

nflverse publishes weekly injury reports:
  https://github.com/nflverse/nflverse-data/releases/download/injuries/injuries_{year}.csv

Each row is one player × one week, with:
  report_primary_injury  — body part (Knee, Hamstring, Concussion, etc.)
  report_status          — Out | Questionable | Doubtful | ""
  practice_status        — "Did Not Participate" | "Limited" | "Full" | ""

This migration aggregates those weekly rows into per-player per-season:
  weeks_out      — weeks listed as report_status = 'Out'
  weeks_limited  — weeks with practice_status containing 'Limited'
  injury_bucket  — 0 none  1 soft-tissue  2 upper-structural
                   3 lower-joint/structural  4 head/neck
  worst_injury   — human-readable label of the most-severe injury seen

Bucket logic (worst single injury drives the bucket):
  Soft tissue (1): hamstring, calf, quad, groin, hip, thigh, oblique,
                   pectoral, abdomen, chest, rib, back, glute, adductor
  Upper (2):       shoulder, wrist, hand, elbow, thumb, finger, forearm,
                   bicep, tricep
  Lower joint (3): knee, ankle, foot, achilles, fibula, heel, toe, plantar
  Head/neck (4):   concussion, neck, head, brain

Combined with weeks_out, bucket 3 + weeks_out ≥ 8 is a reliable ACL proxy.

Run from backend/:
    python db/migrations/014_player_injury_seasons.py
"""

import sys, csv, io, urllib.request
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from db.database import get_connection

ALL_YEARS = list(range(2022, 2026))   # 2022-2025 inclusive
URL_TMPL  = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "injuries/injuries_{year}.csv"
)

# ── Injury bucket taxonomy ───────────────────────────────────────────────────
# Higher bucket = more structurally concerning for future performance.
# Within each bucket, weeks_out captures severity (e.g. ACL vs. knee sprain).

BUCKET_KEYWORDS: dict[int, list[str]] = {
    4: ["concussion", "neck", "head", "brain", "spine", "cervical"],
    3: ["knee", "ankle", "foot", "achilles", "fibula", "heel", "plantar",
        "toe", "tibia", "ligament"],
    2: ["shoulder", "wrist", "hand", "elbow", "thumb", "finger", "forearm",
        "bicep", "tricep", "arm", "collarbone", "clavicle"],
    1: ["hamstring", "calf", "quad", "groin", "hip", "thigh", "oblique",
        "pectoral", "abdomen", "chest", "rib", "back", "glute", "adductor",
        "muscle", "strain", "pull", "hip flexor"],
}

def _classify_injury(text: str) -> int:
    """Return the injury bucket (0–4) for a raw injury string."""
    t = text.lower().strip()
    if not t or t in ("not injury related - personal matter", "illness",
                      "not injury related", "rest", "personal"):
        return 0
    for bucket in (4, 3, 2, 1):          # check worst first
        for kw in BUCKET_KEYWORDS[bucket]:
            if kw in t:
                return bucket
    return 0   # unknown → treat as none


def fetch_csv(year: int) -> Optional[list]:
    url = URL_TMPL.format(year=year)
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            raw = r.read().decode("utf-8", errors="replace")
        return list(csv.DictReader(io.StringIO(raw)))
    except Exception as e:
        print(f"    WARNING: could not fetch {year}: {e}")
        return None


def create_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS player_season_injuries (
            gsis_id          TEXT    NOT NULL,
            season_year      INTEGER NOT NULL,
            weeks_out        INTEGER NOT NULL DEFAULT 0,
            weeks_limited    INTEGER NOT NULL DEFAULT 0,
            injury_bucket    INTEGER NOT NULL DEFAULT 0,
            worst_injury     TEXT,
            PRIMARY KEY (gsis_id, season_year)
        )
    """)
    conn.commit()


def aggregate_and_upsert(conn, year: int, rows: list) -> int:
    """Aggregate weekly injury rows for one season into player-season summaries."""
    from collections import defaultdict

    # per-gsis accumulator
    # { gsis_id: { weeks_out, weeks_limited, worst_bucket, worst_label } }
    acc: dict[str, dict] = defaultdict(lambda: {
        "weeks_out": 0, "weeks_limited": 0,
        "worst_bucket": 0, "worst_label": None,
    })

    for row in rows:
        game_type = (row.get("game_type") or "").upper()
        if game_type not in ("REG", ""):
            continue

        gsis = (row.get("gsis_id") or "").strip()
        pos  = (row.get("position") or "").strip()
        if not gsis or pos not in ("QB", "RB", "WR", "TE"):
            continue

        a = acc[gsis]

        # Count report_status = 'Out'
        if (row.get("report_status") or "").strip() == "Out":
            a["weeks_out"] += 1

        # Count limited practice weeks
        ps = (row.get("practice_status") or "").strip()
        if "Limited" in ps:
            a["weeks_limited"] += 1

        # Derive injury bucket from primary (then secondary) injury field
        raw_primary   = (row.get("report_primary_injury")   or "").strip()
        raw_secondary = (row.get("report_secondary_injury")  or "").strip()
        bucket = max(
            _classify_injury(raw_primary),
            _classify_injury(raw_secondary),
        )
        if bucket > a["worst_bucket"]:
            a["worst_bucket"] = bucket
            a["worst_label"]  = raw_primary or raw_secondary or None

    # Upsert into table
    upserted = 0
    for gsis, a in acc.items():
        conn.execute("""
            INSERT INTO player_season_injuries
                (gsis_id, season_year, weeks_out, weeks_limited, injury_bucket, worst_injury)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (gsis_id, season_year) DO UPDATE SET
                weeks_out     = excluded.weeks_out,
                weeks_limited = excluded.weeks_limited,
                injury_bucket = excluded.injury_bucket,
                worst_injury  = excluded.worst_injury
        """, (gsis, year,
              a["weeks_out"], a["weeks_limited"],
              a["worst_bucket"], a["worst_label"]))
        upserted += 1

    conn.commit()
    return upserted


if __name__ == "__main__":
    conn = get_connection()

    print(f"\n=== Migration 014: Player season injury aggregates ===\n")

    create_table(conn)
    print("Table player_season_injuries created/verified.\n")

    total = 0
    for year in ALL_YEARS:
        print(f"  {year}…", end=" ", flush=True)
        rows = fetch_csv(year)
        if rows is None:
            print("skipped")
            continue
        n = aggregate_and_upsert(conn, year, rows)
        print(f"{len(rows)} weekly rows → {n} player-seasons upserted")
        total += n

    print(f"\nTotal: {total} player-season injury records\n")

    # Spot-check
    print("Sample records (bucket ≥ 3, weeks_out ≥ 4):")
    rows = conn.execute("""
        SELECT psi.gsis_id, psi.season_year, p.full_name, p.position,
               psi.weeks_out, psi.weeks_limited, psi.injury_bucket, psi.worst_injury
        FROM player_season_injuries psi
        LEFT JOIN players p ON p.gsis_id = psi.gsis_id
        WHERE psi.injury_bucket >= 3 AND psi.weeks_out >= 4
        ORDER BY psi.weeks_out DESC
        LIMIT 15
    """).fetchall()
    for r in rows:
        print(f"  {(r['full_name'] or r['gsis_id']):<22} {r['position'] or '?'} "
              f"{r['season_year']}  out={r['weeks_out']}wk  "
              f"bucket={r['injury_bucket']}  {r['worst_injury']}")

    # Irving specifically
    print("\nBucky Irving injury history:")
    rows = conn.execute("""
        SELECT psi.*, p.full_name
        FROM player_season_injuries psi
        JOIN players p ON p.gsis_id = psi.gsis_id
        WHERE p.full_name LIKE '%Bucky Irving%'
    """).fetchall()
    for r in rows:
        print(f"  {r['season_year']}: out={r['weeks_out']}wk  limited={r['weeks_limited']}wk  "
              f"bucket={r['injury_bucket']}  worst={r['worst_injury']}")
    if not rows:
        print("  (no injury data found — played healthy or not in injury reports)")

    conn.close()
    print("\nDone.")
