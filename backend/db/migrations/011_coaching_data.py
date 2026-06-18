"""
Migration 011 — Coaching data from Wikipedia.

Two datasets:

1. team_season_coaching  (HC + OC + DC per team per season, 2000–2025)
   Source: Wikipedia team-season infobox pages, e.g.
   "2022 Kansas City Chiefs season" → | coach = Andy Reid | off_coach = Eric Bieniemy
   Uses batch API: up to 50 pages per HTTP request → ~17 calls total.

2. coach_lineage  (coaching tree relationships)
   Source: Wikipedia individual coach pages, "Coaching tree" sections.
   Covers: Belichick, Reid, McVay, Shanahan (Kyle + Mike), Walsh/WCO tree,
           Payton, Carroll, Harbaugh, McCarthy, LaFleur (Matt)

Derived features added to team_season_scheme:
   hc_name, hc_tenure, oc_name, oc_tenure, new_oc, coaching_tree

coaching_tree values:
  'shanahan_mcvay'  — Walsh → M.Shanahan → McVay / K.Shanahan lineage
  'belichick'       — Belichick disciples
  'reid_wco'        — Andy Reid / West Coast Office lineage
  'payton_no'       — Sean Payton disciples
  'other'           — everything else
"""

import sys
import re
import time
import json
import urllib.request
import urllib.parse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from db.database import get_connection

WIKI_UA = (
    "FFAnalyzer/1.0 "
    "(https://github.com/fantasy-football-analyzer; research project)"
)
WIKI_BATCH_SIZE = 48          # stay under the 50-title limit
WIKI_SLEEP      = 1.0         # seconds between batches (Wikipedia asks for ≥ 1s)

# ── Team name mapping ──────────────────────────────────────────────────────
TEAM_WIKI_NAMES = {
    "ARI": "Arizona Cardinals",   "ATL": "Atlanta Falcons",
    "BAL": "Baltimore Ravens",    "BUF": "Buffalo Bills",
    "CAR": "Carolina Panthers",   "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals",  "CLE": "Cleveland Browns",
    "DAL": "Dallas Cowboys",      "DEN": "Denver Broncos",
    "DET": "Detroit Lions",       "GB":  "Green Bay Packers",
    "HOU": "Houston Texans",      "IND": "Indianapolis Colts",
    "JAX": "Jacksonville Jaguars","KC":  "Kansas City Chiefs",
    "LAC": "Los Angeles Chargers","LA":  "Los Angeles Rams",
    "LV":  "Las Vegas Raiders",   "MIA": "Miami Dolphins",
    "MIN": "Minnesota Vikings",   "NE":  "New England Patriots",
    "NO":  "New Orleans Saints",  "NYG": "New York Giants",
    "NYJ": "New York Jets",       "PHI": "Philadelphia Eagles",
    "PIT": "Pittsburgh Steelers", "SEA": "Seattle Seahawks",
    "SF":  "San Francisco 49ers", "TB":  "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans",    "WAS": "Washington Commanders",
}

# Year-specific overrides for relocated / renamed teams
def team_name_for(abbr: str, year: int) -> str:
    overrides = {
        "LA":  {y: "St. Louis Rams"       for y in range(2000, 2016)},
        "LAC": {y: "San Diego Chargers"   for y in range(2000, 2017)},
        "LV":  {y: "Oakland Raiders"      for y in range(2000, 2020)},
        "WAS": {2020: "Washington Football Team",
                2021: "Washington Football Team",
                2022: "Washington Commanders"},
    }
    team_map = overrides.get(abbr, {})
    return team_map.get(year, TEAM_WIKI_NAMES[abbr])


# ── Coach lineage list ─────────────────────────────────────────────────────
LINEAGE_COACHES = [
    # (wikipedia_page_title, tree_tag, is_root)
    ("Bill Walsh",          "shanahan_mcvay", True),
    ("Mike Shanahan",       "shanahan_mcvay", False),
    ("Kyle Shanahan",       "shanahan_mcvay", False),
    ("Sean McVay",          "shanahan_mcvay", False),
    ("Pete Carroll",        "shanahan_mcvay", False),
    ("Matt LaFleur",        "shanahan_mcvay", False),
    ("Zac Taylor",          "shanahan_mcvay", False),
    ("Kevin O'Connell (American football)", "shanahan_mcvay", False),
    ("Bill Belichick",      "belichick",      True),
    ("Nick Saban",          "belichick",      False),
    ("Josh McDaniels",      "belichick",      False),
    ("Brian Flores",        "belichick",      False),
    ("Andy Reid",           "reid_wco",       True),
    ("Mike McCarthy",       "reid_wco",       False),
    ("Doug Pederson",       "reid_wco",       False),
    ("Sean Payton",         "payton_no",      True),
    ("John Harbaugh",       "belichick",      False),
]


CREATE_COACHING = """
CREATE TABLE IF NOT EXISTS team_season_coaching (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    season_year INTEGER NOT NULL,
    team        TEXT    NOT NULL,
    hc_name     TEXT,
    oc_name     TEXT,
    dc_name     TEXT,
    UNIQUE (season_year, team)
);
"""

CREATE_LINEAGE = """
CREATE TABLE IF NOT EXISTS coach_lineage (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    coach_name  TEXT NOT NULL,
    mentored_by TEXT,
    tree_root   TEXT,
    tree_tag    TEXT,
    UNIQUE (coach_name)
);
"""

ADD_COACHING_COLS = [
    "ALTER TABLE team_season_scheme ADD COLUMN hc_name       TEXT",
    "ALTER TABLE team_season_scheme ADD COLUMN hc_tenure     INTEGER",
    "ALTER TABLE team_season_scheme ADD COLUMN oc_name       TEXT",
    "ALTER TABLE team_season_scheme ADD COLUMN oc_tenure     INTEGER",
    "ALTER TABLE team_season_scheme ADD COLUMN new_oc        INTEGER DEFAULT 0",
    "ALTER TABLE team_season_scheme ADD COLUMN coaching_tree TEXT    DEFAULT 'other'",
]


# ── Wikipedia helpers ──────────────────────────────────────────────────────

def wiki_batch(titles: list) -> dict:
    """
    Fetch wikitext for up to WIKI_BATCH_SIZE page titles in one API call.
    Returns {normalised_title: content_str}. Missing pages map to ''.
    Retries once on 429 with a 60s back-off.
    """
    batch_str = "|".join(titles)
    url = (
        "https://en.wikipedia.org/w/api.php"
        "?action=query&prop=revisions&rvprop=content"
        "&format=json&formatversion=2&titles="
        + urllib.parse.quote(batch_str)
    )
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": WIKI_UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read())
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                print("    429 rate-limit — sleeping 60s…")
                time.sleep(60)
                continue
            return {}
        except Exception:
            return {}

    result = {}
    for page in data.get("query", {}).get("pages", []):
        title = page.get("title", "")
        if page.get("missing"):
            result[title] = ""
        else:
            revs = page.get("revisions", [{}])
            result[title] = revs[0].get("content", "") if revs else ""
    return result


def clean_wiki_name(raw: str) -> str:
    if not raw:
        return ""
    raw = re.sub(r"\[\[(?:[^\]|]+\|)?([^\]]+)\]\]", r"\1", raw)
    raw = re.sub(r"[{}<>\[\]]", "", raw)
    return raw.strip().split("\n")[0].strip()


def parse_infobox(content: str, field: str) -> str:
    m = re.search(r"\|\s*" + re.escape(field) + r"\s*=\s*(.+)", content)
    return clean_wiki_name(m.group(1)) if m else ""


def parse_coaching_tree(content: str) -> list:
    m = re.search(
        r"==+\s*[Cc]oaching [Tt]ree\s*==+(.*?)(?:\n==|\Z)", content, re.DOTALL
    )
    if not m:
        return []
    raw_names = re.findall(
        r"\*[^\n]*\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]", m.group(1)
    )
    out = []
    for name in raw_names:
        if re.search(r"\d{4}|Category:|File:|List of", name):
            continue
        display = re.sub(r"\s*\([^)]+\)", "", name).strip()
        if display and len(display) > 3:
            out.append(display)
    return list(dict.fromkeys(out))


# ── Step 1: team-season coaching ──────────────────────────────────────────

def scrape_coaching(years: list, conn):
    teams  = list(TEAM_WIKI_NAMES.keys())
    # Build full list of (abbr, year, wiki_title)
    requests = []
    for year in years:
        for abbr in teams:
            name  = team_name_for(abbr, year)
            title = f"{year} {name} season"
            requests.append((abbr, year, title))

    # Batch them
    batches = [requests[i:i + WIKI_BATCH_SIZE]
               for i in range(0, len(requests), WIKI_BATCH_SIZE)]

    total_ok = total_miss = 0
    year_ok  = {}
    year_miss = {}

    print(f"  {len(requests)} pages → {len(batches)} batch requests…")

    for batch_num, batch in enumerate(batches, 1):
        titles = [r[2] for r in batch]
        pages  = wiki_batch(titles)
        time.sleep(WIKI_SLEEP)

        for abbr, year, title in batch:
            content = pages.get(title, "")
            if not content:
                year_miss[year] = year_miss.get(year, 0) + 1
                total_miss += 1
                continue

            hc = parse_infobox(content, "coach")
            oc = parse_infobox(content, "off_coach")
            dc = parse_infobox(content, "def_coach")

            if not hc:
                year_miss[year] = year_miss.get(year, 0) + 1
                total_miss += 1
                continue

            conn.execute("""
                INSERT INTO team_season_coaching
                    (season_year, team, hc_name, oc_name, dc_name)
                VALUES (?,?,?,?,?)
                ON CONFLICT (season_year, team) DO UPDATE SET
                    hc_name = excluded.hc_name,
                    oc_name = excluded.oc_name,
                    dc_name = excluded.dc_name
            """, (year, abbr, hc or None, oc or None, dc or None))
            year_ok[year] = year_ok.get(year, 0) + 1
            total_ok += 1

        if batch_num % 5 == 0 or batch_num == len(batches):
            print(f"    batch {batch_num}/{len(batches)}  ok={total_ok}  miss={total_miss}")

    conn.commit()

    # Per-year summary
    print("\n  Per-year coverage:")
    for year in years:
        ok   = year_ok.get(year, 0)
        miss = year_miss.get(year, 0)
        bar  = "█" * ok + "░" * miss
        print(f"    {year}: {ok:>2} ok  {miss:>2} miss  {bar}")

    return total_ok, total_miss


# ── Step 2: coaching tree lineage ─────────────────────────────────────────

def scrape_lineage(conn):
    titles = [name for name, _, _ in LINEAGE_COACHES]
    pages  = wiki_batch(titles)
    time.sleep(WIKI_SLEEP)

    inserted = 0
    for page_title, tree_tag, is_root in LINEAGE_COACHES:
        display = re.sub(r"\s*\([^)]+\)", "", page_title).strip()
        content = pages.get(page_title, "")
        if not content:
            print(f"  MISS: {display}")
            continue

        disciples = parse_coaching_tree(content)

        # Upsert the coach themselves
        tree_root_val = display if is_root else None
        conn.execute("""
            INSERT INTO coach_lineage (coach_name, mentored_by, tree_root, tree_tag)
            VALUES (?,?,?,?)
            ON CONFLICT (coach_name) DO UPDATE SET
                tree_root = COALESCE(excluded.tree_root, coach_lineage.tree_root),
                tree_tag  = excluded.tree_tag
        """, (display, None, tree_root_val, tree_tag))

        for disc in disciples:
            conn.execute("""
                INSERT INTO coach_lineage (coach_name, mentored_by, tree_root, tree_tag)
                VALUES (?,?,?,?)
                ON CONFLICT (coach_name) DO UPDATE SET
                    mentored_by = excluded.mentored_by,
                    tree_tag    = COALESCE(coach_lineage.tree_tag, excluded.tree_tag)
            """, (disc, display, tree_root_val, tree_tag))
            inserted += 1

        conn.commit()
        print(f"  {display:30} ({tree_tag:15}): {len(disciples)} disciples"
              f"  → {disciples[:4]}")

    return inserted


# ── Step 3: derive tenure / new_oc / tree ─────────────────────────────────

def compute_derived(conn):
    # Copy hc / oc names from coaching table into scheme table
    conn.execute("""
        UPDATE team_season_scheme AS tss
        SET hc_name = (
            SELECT tsc.hc_name FROM team_season_coaching tsc
            WHERE tsc.season_year = tss.season_year AND tsc.team = tss.team
        ),
        oc_name = (
            SELECT tsc.oc_name FROM team_season_coaching tsc
            WHERE tsc.season_year = tss.season_year AND tsc.team = tss.team
        )
    """)

    # HC tenure: consecutive seasons same HC at same team up to this year
    conn.execute("""
        UPDATE team_season_scheme AS tss
        SET hc_tenure = (
            SELECT COUNT(*) FROM team_season_coaching tsc2
            WHERE tsc2.team     = tss.team
              AND tsc2.hc_name  = tss.hc_name
              AND tsc2.season_year <= tss.season_year
        )
        WHERE tss.hc_name IS NOT NULL
    """)

    # OC tenure
    conn.execute("""
        UPDATE team_season_scheme AS tss
        SET oc_tenure = (
            SELECT COUNT(*) FROM team_season_coaching tsc2
            WHERE tsc2.team     = tss.team
              AND tsc2.oc_name  = tss.oc_name
              AND tsc2.season_year <= tss.season_year
        )
        WHERE tss.oc_name IS NOT NULL
    """)

    # new_oc flag
    conn.execute("""
        UPDATE team_season_scheme AS tss
        SET new_oc = (
            SELECT CASE
                WHEN prev.oc_name IS NULL THEN 0
                WHEN tss.oc_name  IS NULL THEN 0
                WHEN prev.oc_name != tss.oc_name THEN 1
                ELSE 0
            END
            FROM team_season_scheme prev
            WHERE prev.team        = tss.team
              AND prev.season_year = tss.season_year - 1
        )
        WHERE tss.oc_name IS NOT NULL
    """)

    # coaching_tree: HC lookup first, then OC, then 'other'
    conn.execute("""
        UPDATE team_season_scheme AS tss
        SET coaching_tree = COALESCE(
            (SELECT cl.tree_tag FROM coach_lineage cl WHERE cl.coach_name = tss.hc_name),
            (SELECT cl.tree_tag FROM coach_lineage cl WHERE cl.coach_name = tss.oc_name),
            'other'
        )
    """)

    conn.commit()


# ── main ──────────────────────────────────────────────────────────────────

def run():
    conn = get_connection()

    conn.execute(CREATE_COACHING)
    conn.execute(CREATE_LINEAGE)
    for sql in ADD_COACHING_COLS:
        try:
            conn.execute(sql)
        except Exception:
            pass
    conn.commit()

    years = list(range(2000, 2026))

    print("Step 1: Scraping team-season coaching from Wikipedia…")
    ok, miss = scrape_coaching(years, conn)
    print(f"\nTeam-season coaching: {ok} rows, {miss} misses\n")

    print("Step 2: Scraping coaching tree lineages…")
    n_disc = scrape_lineage(conn)
    print(f"\nCoach lineage: {n_disc} disciple relationships\n")

    print("Step 3: Computing derived features…")
    compute_derived(conn)

    # ── Summary ────────────────────────────────────────────────────────────
    print("\nSample 2022 coaching:")
    rows = conn.execute("""
        SELECT tss.season_year, tss.team,
               tss.hc_name, tss.hc_tenure,
               tss.oc_name, tss.oc_tenure, tss.new_oc,
               tss.coaching_tree
        FROM team_season_scheme tss
        WHERE tss.season_year = 2022
          AND tss.hc_name IS NOT NULL
        ORDER BY tss.coaching_tree, tss.team
        LIMIT 16
    """).fetchall()
    for r in rows:
        print(f"  {r['team']:>4}  HC: {(r['hc_name'] or '?'):22} yr={r['hc_tenure'] or '?':>2}  "
              f"OC: {(r['oc_name'] or '?'):22} yr={r['oc_tenure'] or '?':>2}  "
              f"new={r['new_oc']}  tree={r['coaching_tree']}")

    print("\nCoaching tree distribution (2022):")
    for r in conn.execute("""
        SELECT coaching_tree, COUNT(*) n FROM team_season_scheme
        WHERE season_year=2022 GROUP BY coaching_tree ORDER BY n DESC
    """).fetchall():
        print(f"  {r['coaching_tree']:20} {r['n']:>3}")

    conn.close()
    print("\nDone. Run 012_personnel_data.py next.")


if __name__ == "__main__":
    run()
