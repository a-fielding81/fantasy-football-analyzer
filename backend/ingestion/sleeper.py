"""
Sleeper API ingestion.

Sleeper uses a new league_id each season. The root league_id is the most
recent; previous seasons are discovered by walking previous_league_id links.
All API calls are unauthenticated — Sleeper's API is fully public.
"""

from __future__ import annotations

import time
import requests
from db.database import get_connection, upsert

BASE = "https://api.sleeper.app/v1"
LEAGUE_ID = "1312965631825428480"

POSITION_MAP = {
    "QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE",
    "K": "K", "DEF": "DEF", "DL": "DL", "LB": "LB",
    "DB": "DB", "FLEX": "FLEX", "SUPER_FLEX": "SUPER_FLEX",
    "IDP_FLEX": "IDP",
}


# ── Low-level helpers ──────────────────────────────────────────────────────────

def _get(path: str, retries: int = 3) -> dict | list:
    url = f"{BASE}{path}"
    for attempt in range(retries):
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        resp.raise_for_status()
    raise RuntimeError(f"Failed GET {url} after {retries} attempts")


def _normalize_position(pos: str | None) -> str:
    if not pos:
        return "UNKNOWN"
    return POSITION_MAP.get(pos.upper(), "UNKNOWN")


# ── Season discovery ───────────────────────────────────────────────────────────

def get_all_season_ids(root_league_id: str = LEAGUE_ID) -> list[dict]:
    """Walk previous_league_id links to collect all Sleeper seasons."""
    seasons = []
    league_id = root_league_id
    while league_id:
        info = _get(f"/league/{league_id}")
        seasons.append({
            "platform_season_id": league_id,
            "year": int(info["season"]),
            "draft_id": info.get("draft_id"),
            "name": info.get("name", ""),
            "previous_league_id": info.get("previous_league_id"),
        })
        prev = info.get("previous_league_id")
        league_id = prev if prev and str(prev) not in ("0", "None") else None
    seasons.sort(key=lambda s: s["year"])
    return seasons


# ── Core ingestion ─────────────────────────────────────────────────────────────

def ingest_players(conn) -> dict[str, int]:
    """Bulk-load all Sleeper player metadata. Returns sleeper_id → db id map."""
    print("  Fetching Sleeper player database...")
    all_players = _get("/players/nfl")
    inserted = 0
    id_map: dict[str, int] = {}

    for sleeper_id, p in all_players.items():
        if not p.get("full_name") and not (p.get("first_name") and p.get("last_name")):
            continue
        full_name = p.get("full_name") or f"{p.get('first_name','')} {p.get('last_name','')}".strip()
        row = {
            "sleeper_id": sleeper_id,
            "full_name": full_name,
            "first_name": p.get("first_name"),
            "last_name": p.get("last_name"),
            "position": _normalize_position(p.get("position")),
            "nfl_team": p.get("team"),
            "birth_date": p.get("birth_date"),
            "years_exp": p.get("years_exp"),
            "status": p.get("status"),
        }
        db_id = upsert(conn, "players", row, ["sleeper_id"])
        id_map[sleeper_id] = db_id
        inserted += 1

    conn.commit()
    print(f"  {inserted} players upserted.")
    return id_map


def ingest_league_season(conn, season_info: dict, db_league_id: int, player_map: dict[str, int]) -> int:
    """Ingest one Sleeper season. Returns db season id."""
    year = season_info["year"]
    platform_season_id = season_info["platform_season_id"]
    print(f"\n  ── Season {year} (Sleeper id: {platform_season_id}) ──")

    # Season row
    season_id = upsert(conn, "seasons", {
        "league_id": db_league_id,
        "year": year,
        "platform": "sleeper",
        "platform_season_id": platform_season_id,
    }, ["league_id", "year"])

    # Users & rosters
    users = {u["user_id"]: u for u in _get(f"/league/{platform_season_id}/users")}
    rosters = _get(f"/league/{platform_season_id}/rosters")

    team_map: dict[str, int] = {}  # roster_id → db team id

    for roster in rosters:
        owner_id = roster.get("owner_id")
        user = users.get(owner_id, {})
        display_name = user.get("display_name", f"Unknown-{owner_id}")

        manager_id = upsert(conn, "managers", {
            "display_name": display_name,
            "sleeper_user_id": owner_id,
        }, ["sleeper_user_id"])

        settings = roster.get("settings", {})
        team_id = upsert(conn, "teams", {
            "season_id": season_id,
            "manager_id": manager_id,
            "platform_team_id": str(roster["roster_id"]),
            "wins": settings.get("wins", 0),
            "losses": settings.get("losses", 0),
            "ties": settings.get("ties", 0),
            "points_for": settings.get("fpts", 0) + settings.get("fpts_decimal", 0) / 100,
            "points_against": settings.get("fpts_against", 0) + settings.get("fpts_against_decimal", 0) / 100,
        }, ["season_id", "platform_team_id"])

        team_map[str(roster["roster_id"])] = team_id
        print(f"    Team: {display_name} ({roster['roster_id']})")

    conn.commit()

    # Draft
    _ingest_draft(conn, season_info, season_id, team_map, player_map)

    # Transactions (trades)
    _ingest_transactions(conn, platform_season_id, season_id, team_map, player_map)

    # Weekly scores & stats
    _ingest_weekly_scores(conn, platform_season_id, season_id, team_map, player_map)

    return season_id


def _ingest_draft(conn, season_info: dict, season_id: int, team_map: dict, player_map: dict):
    draft_id = season_info.get("draft_id")
    if not draft_id:
        print("    No draft_id found, skipping draft.")
        return

    try:
        picks = _get(f"/draft/{draft_id}/picks")
    except Exception as e:
        print(f"    Draft fetch failed: {e}")
        return

    print(f"    Ingesting {len(picks)} draft picks...")
    for pick in picks:
        roster_id = str(pick.get("roster_id", ""))
        team_id = team_map.get(roster_id)
        if not team_id:
            continue
        sleeper_id = pick.get("player_id")
        player_id = player_map.get(sleeper_id) if sleeper_id else None
        metadata = pick.get("metadata", {})
        is_keeper = 1 if metadata.get("is_keeper") in ("1", True, 1) else 0
        upsert(conn, "draft_picks", {
            "season_id": season_id,
            "team_id": team_id,
            "player_id": player_id,
            "round": pick["round"],
            "pick_number": pick["pick_no"],
            "pick_in_round": pick["draft_slot"],
            "is_keeper": is_keeper,
            "platform_pick_id": str(pick.get("pick_id", "")),
        }, ["season_id", "pick_number"])
    conn.commit()


def _ingest_transactions(conn, platform_season_id: str, season_id: int, team_map: dict, player_map: dict):
    all_trades = []
    for week in range(1, 19):
        try:
            txns = _get(f"/league/{platform_season_id}/transactions/{week}")
        except Exception:
            break
        trades = [t for t in txns if t.get("type") == "trade" and t.get("status") == "complete"]
        all_trades.extend([(week, t) for t in trades])

    print(f"    Ingesting {len(all_trades)} trades...")

    for week, txn in all_trades:
        platform_id = str(txn.get("transaction_id", ""))
        trade_id = upsert(conn, "trades", {
            "season_id": season_id,
            "week": week,
            "transaction_date": txn.get("status_updated"),
            "platform_transaction_id": platform_id,
            "status": "completed",
        }, ["platform_transaction_id"])

        adds: dict = txn.get("adds") or {}       # player_id → roster_id
        drops: dict = txn.get("drops") or {}
        draft_picks: list = txn.get("draft_picks") or []
        roster_ids: list = txn.get("roster_ids") or []

        # Build roster_id → [players received] map
        received: dict[str, list] = {str(r): [] for r in roster_ids}
        for sleeper_pid, roster_id in adds.items():
            received.setdefault(str(roster_id), []).append(sleeper_pid)

        # Determine sending side: player was dropped from one roster and added to another
        sent_from: dict[str, str] = {}  # sleeper_pid → sending roster_id
        for sleeper_pid, roster_id in drops.items():
            sent_from[sleeper_pid] = str(roster_id)

        for sleeper_pid, recv_roster_id in adds.items():
            recv_roster_id = str(recv_roster_id)
            send_roster_id = sent_from.get(sleeper_pid)
            if not send_roster_id:
                continue
            receiving_team = team_map.get(recv_roster_id)
            sending_team = team_map.get(send_roster_id)
            if not receiving_team or not sending_team:
                continue
            player_id = player_map.get(sleeper_pid)
            conn.execute(
                """INSERT OR IGNORE INTO trade_assets
                   (trade_id, sending_team_id, receiving_team_id, asset_type, player_id)
                   VALUES (?, ?, ?, 'player', ?)""",
                (trade_id, sending_team, receiving_team, player_id),
            )

        for dp in draft_picks:
            send_roster_id = str(dp.get("previous_owner_id", ""))
            recv_roster_id = str(dp.get("owner_id", ""))
            sending_team = team_map.get(send_roster_id)
            receiving_team = team_map.get(recv_roster_id)
            if not sending_team or not receiving_team:
                continue
            orig_roster_id = str(dp.get("roster_id", send_roster_id))
            conn.execute(
                """INSERT OR IGNORE INTO trade_assets
                   (trade_id, sending_team_id, receiving_team_id, asset_type,
                    pick_season_year, pick_round, pick_original_team_id)
                   VALUES (?, ?, ?, 'draft_pick', ?, ?, ?)""",
                (trade_id, sending_team, receiving_team,
                 dp.get("season"), dp.get("round"), team_map.get(orig_roster_id)),
            )

    conn.commit()


def _ingest_weekly_scores(conn, platform_season_id: str, season_id: int, team_map: dict, player_map: dict):
    print("    Ingesting weekly matchup scores...")
    for week in range(1, 19):
        try:
            matchups = _get(f"/league/{platform_season_id}/matchups/{week}")
        except Exception:
            break
        if not matchups:
            break

        # Group by matchup_id to find opponents
        groups: dict[int, list] = {}
        for m in matchups:
            groups.setdefault(m["matchup_id"], []).append(m)

        for matchup_id, pair in groups.items():
            for i, entry in enumerate(pair):
                roster_id = str(entry.get("roster_id", ""))
                team_id = team_map.get(roster_id)
                if not team_id:
                    continue
                opponent = pair[1 - i] if len(pair) == 2 else None
                upsert(conn, "weekly_scores", {
                    "season_id": season_id,
                    "team_id": team_id,
                    "week": week,
                    "points_scored": entry.get("points", 0),
                    "points_against": opponent["points"] if opponent else None,
                    "matchup_id": str(matchup_id),
                }, ["season_id", "team_id", "week"])

            # Player stats from starters/players on each roster
            for entry in pair:
                roster_id = str(entry.get("roster_id", ""))
                players_pts: dict = entry.get("players_points") or {}
                for sleeper_pid, pts in players_pts.items():
                    player_id = player_map.get(sleeper_pid)
                    if not player_id:
                        continue
                    # Upsert — later weeks won't clobber earlier inserts
                    conn.execute(
                        """INSERT INTO player_weekly_stats
                           (season_id, player_id, week, fantasy_points)
                           VALUES (?, ?, ?, ?)
                           ON CONFLICT (season_id, player_id, week)
                           DO UPDATE SET fantasy_points = MAX(fantasy_points, excluded.fantasy_points)
                        """,
                        (season_id, player_id, week, pts),
                    )
        conn.commit()


# ── Entry point ────────────────────────────────────────────────────────────────

def run_ingestion(root_league_id: str = LEAGUE_ID):
    print("=== Sleeper Ingestion ===")
    conn = get_connection()

    # Ensure top-level league row exists
    db_league_id = upsert(conn, "leagues", {
        "platform": "sleeper",
        "platform_id": root_league_id,
        "name": "Fantasy Football League",
        "scoring_format": "half_ppr",
        "team_count": 10,
        "keepers_per_team": 6,
    }, ["platform", "platform_id"])
    conn.commit()

    # Load all players once
    player_map = ingest_players(conn)

    # Walk all seasons
    seasons = get_all_season_ids(root_league_id)
    print(f"\nFound {len(seasons)} Sleeper season(s): {[s['year'] for s in seasons]}")

    for s in seasons:
        try:
            ingest_league_season(conn, s, db_league_id, player_map)
            conn.execute(
                "INSERT INTO ingestion_log (platform, season_year, records_inserted, status) VALUES (?,?,?,?)",
                ("sleeper", s["year"], 0, "success"),
            )
        except Exception as e:
            print(f"  ERROR on season {s['year']}: {e}")
            conn.execute(
                "INSERT INTO ingestion_log (platform, season_year, status, notes) VALUES (?,?,?,?)",
                ("sleeper", s["year"], "failed", str(e)),
            )
        conn.commit()

    conn.close()
    print("\n=== Sleeper ingestion complete ===")
