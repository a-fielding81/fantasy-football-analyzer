"""
ESPN ingestion via the espn-api library.

The library handles auth automatically for public leagues.
For private leagues, supply espn_s2 and swid cookies from a
logged-in browser session.
"""

from __future__ import annotations

from espn_api.football import League
from db.database import get_connection, upsert

ESPN_LEAGUE_ID = 92157291
ESPN_YEARS = [2021, 2022]   # Adjust if the league ran more seasons on ESPN

POSITION_MAP = {
    "QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE",
    "K": "K",  "D/ST": "DEF", "DEF": "DEF",
    "FLEX": "FLEX", "BE": "UNKNOWN", "IR": "UNKNOWN",
}


def _normalize_pos(pos: str | None) -> str:
    if not pos:
        return "UNKNOWN"
    return POSITION_MAP.get(pos.upper(), "UNKNOWN")


def ingest_espn_season(conn, league: League, year: int, db_league_id: int):
    print(f"\n  ── ESPN Season {year} ──")

    season_id = upsert(conn, "seasons", {
        "league_id": db_league_id,
        "year": year,
        "platform": "espn",
        "platform_season_id": str(ESPN_LEAGUE_ID),
    }, ["league_id", "year"])

    player_map: dict[str, int] = {}   # espn playerId (str) → db id
    team_map: dict[int, int] = {}     # espn teamId → db team id

    # Teams & managers
    for team in league.teams:
        owner_name = " ".join(filter(None, [
            getattr(team, "owner", None),
            getattr(team, "co_owners", [None])[0] if getattr(team, "co_owners", None) else None,
        ])).strip() or f"Owner-{team.team_id}"
        # ESPN doesn't expose a stable user id — use owner name as key
        manager_id = upsert(conn, "managers", {
            "display_name":  owner_name,
            "espn_owner_id": str(ESPN_LEAGUE_ID) + "_" + str(team.team_id),
        }, ["espn_owner_id"])

        settings = {}
        wins   = getattr(team, "wins", 0)
        losses = getattr(team, "losses", 0)
        ties   = getattr(team, "ties", 0)
        pf     = getattr(team, "points_for", 0)
        pa     = getattr(team, "points_against", 0)

        db_team_id = upsert(conn, "teams", {
            "season_id": season_id,
            "manager_id": manager_id,
            "platform_team_id": str(team.team_id),
            "team_name": team.team_name,
            "wins": wins,
            "losses": losses,
            "ties": ties,
            "points_for": pf,
            "points_against": pa,
        }, ["season_id", "platform_team_id"])

        team_map[team.team_id] = db_team_id
        print(f"    Team: {team.team_name} ({owner_name})")

        # Roster players
        for player in getattr(team, "roster", []):
            p_id = str(getattr(player, "playerId", None) or getattr(player, "name", ""))
            if not p_id:
                continue
            db_player_id = upsert(conn, "players", {
                "espn_id": p_id,
                "full_name": player.name,
                "position": _normalize_pos(getattr(player, "position", None)),
                "nfl_team": getattr(player, "proTeam", None),
            }, ["espn_id"])
            player_map[p_id] = db_player_id

            upsert(conn, "roster_players", {
                "season_id": season_id,
                "team_id": db_team_id,
                "player_id": db_player_id,
                "week": 0,
                "acquisition_type": "unknown",
            }, ["season_id", "team_id", "player_id", "week"])

    conn.commit()

    # Draft
    try:
        draft = league.draft
        print(f"    Ingesting {len(draft)} draft picks...")
        for pick in draft:
            p_id = str(getattr(pick.playerName, "__class__", None) and pick.playerId or getattr(pick, "playerId", ""))
            p_name = getattr(pick, "playerName", None) or str(pick)
            db_player_id = None
            if p_id:
                db_player_id = upsert(conn, "players", {
                    "espn_id": str(p_id),
                    "full_name": p_name,
                    "position": "UNKNOWN",
                }, ["espn_id"])
                player_map[str(p_id)] = db_player_id

            picking_team = team_map.get(getattr(pick, "team", {team_id: None} if False else 0) if False else pick.team.team_id if hasattr(pick, "team") else 0)
            if not picking_team:
                continue

            round_num = getattr(pick, "round_num", None) or getattr(pick, "roundNum", 1)
            round_pick = getattr(pick, "round_pick", None) or getattr(pick, "roundPick", 1)
            pick_no = (round_num - 1) * len(league.teams) + round_pick

            upsert(conn, "draft_picks", {
                "season_id": season_id,
                "team_id": picking_team,
                "player_id": db_player_id,
                "round": round_num,
                "pick_number": pick_no,
                "pick_in_round": round_pick,
                "is_keeper": 0,
            }, ["season_id", "pick_number"])
        conn.commit()
    except Exception as e:
        print(f"    Draft ingestion failed: {e}")

    # Weekly scores
    try:
        print("    Ingesting weekly matchup scores...")
        for week in range(1, 18):
            try:
                box_scores = league.box_scores(week)
            except Exception:
                break
            for box in box_scores:
                for side, opp in [(box.home_team, box.away_team), (box.away_team, box.home_team)]:
                    if not side:
                        continue
                    team_id = team_map.get(side.team_id)
                    if not team_id:
                        continue
                    opp_score = getattr(opp, "score", None) if opp else None
                    upsert(conn, "weekly_scores", {
                        "season_id": season_id,
                        "team_id": team_id,
                        "week": week,
                        "points_scored": getattr(side, "score", 0),
                        "points_against": opp_score,
                        "matchup_id": f"{week}-{min(side.team_id, opp.team_id if opp else 0)}-{max(side.team_id, opp.team_id if opp else 0)}",
                    }, ["season_id", "team_id", "week"])

                    # Player-level stats from lineup
                    lineup = getattr(side, "lineup", []) or []
                    for player in lineup:
                        p_espn_id = str(getattr(player, "playerId", "") or getattr(player, "name", ""))
                        pts = getattr(player, "points", 0)
                        db_player_id = player_map.get(p_espn_id)
                        if not db_player_id:
                            db_player_id = upsert(conn, "players", {
                                "espn_id": p_espn_id,
                                "full_name": getattr(player, "name", "Unknown"),
                                "position": _normalize_pos(getattr(player, "position", None)),
                            }, ["espn_id"])
                            player_map[p_espn_id] = db_player_id
                        conn.execute(
                            """INSERT INTO player_weekly_stats
                               (season_id, player_id, week, fantasy_points)
                               VALUES (?, ?, ?, ?)
                               ON CONFLICT (season_id, player_id, week)
                               DO UPDATE SET fantasy_points = MAX(fantasy_points, excluded.fantasy_points)
                            """,
                            (season_id, db_player_id, week, pts),
                        )
            conn.commit()
    except Exception as e:
        print(f"    Weekly scores ingestion failed: {e}")


def run_ingestion(espn_s2: str | None = None, swid: str | None = None):
    print("=== ESPN Ingestion ===")
    conn = get_connection()

    db_league_id = upsert(conn, "leagues", {
        "platform": "espn",
        "platform_id": str(ESPN_LEAGUE_ID),
        "name": "Fantasy Football League (ESPN)",
        "scoring_format": "half_ppr",
        "team_count": 10,
        "keepers_per_team": 6,
    }, ["platform", "platform_id"])
    conn.commit()

    for year in ESPN_YEARS:
        try:
            kwargs = {"league_id": ESPN_LEAGUE_ID, "year": year}
            if espn_s2 and swid:
                kwargs["espn_s2"] = espn_s2
                kwargs["swid"] = swid
            league = League(**kwargs)
            ingest_espn_season(conn, league, year, db_league_id)
            conn.execute(
                "INSERT INTO ingestion_log (platform, season_year, status) VALUES (?,?,?)",
                ("espn", year, "success"),
            )
        except Exception as e:
            print(f"  ERROR on ESPN season {year}: {e}")
            conn.execute(
                "INSERT INTO ingestion_log (platform, season_year, status, notes) VALUES (?,?,?,?)",
                ("espn", year, "failed", str(e)),
            )
        conn.commit()

    conn.close()
    print("\n=== ESPN ingestion complete ===")
