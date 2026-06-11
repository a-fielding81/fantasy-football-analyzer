from typing import Optional
from fastapi import APIRouter, Query
from db.database import get_connection

router = APIRouter()


# ---------------------------------------------------------------------------
# Grading helpers
# ---------------------------------------------------------------------------

def _grade_share(share: float) -> str:
    """Convert a side's value share (0–1) into a letter grade."""
    if share >= 0.68:   return "A+"
    if share >= 0.62:   return "A"
    if share >= 0.57:   return "B+"
    if share >= 0.52:   return "B"
    if share >= 0.47:   return "C"   # roughly even
    if share >= 0.42:   return "D"
    if share >= 0.35:   return "F"
    return "F-"


def _grade_label(share: float) -> str:
    if share >= 0.62: return "Won"
    if share >= 0.47: return "Even"
    return "Lost"


@router.get("/")
def list_trades(year: Optional[int] = Query(None)):
    conn = get_connection()
    where = "WHERE s.year = ?" if year else ""
    params = (year,) if year else ()
    rows = conn.execute(f"""
        SELECT DISTINCT
            t.id          AS trade_id,
            s.year,
            t.week,
            t.transaction_date,
            t.status
        FROM trades t
        JOIN seasons s ON s.id = t.season_id
        {where}
        ORDER BY s.year, t.week, t.id
    """, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.get("/detail")
def trade_detail(year: Optional[int] = Query(None)):
    """Full asset-level detail for all trades, optionally filtered by year."""
    conn = get_connection()
    where = "WHERE year = ?" if year else ""
    params = (year,) if year else ()
    rows = conn.execute(f"""
        SELECT * FROM v_trade_detail
        {where}
        ORDER BY year, trade_week, trade_id, asset_type
    """, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.get("/grades")
def trade_grades(year: Optional[int] = Query(None)):
    """
    Grade every trade by comparing post-trade fantasy value received by each side.

    For players: sum their fantasy_points across all seasons >= trade season.
    For draft picks: sum fantasy_points of the player actually drafted with that pick.

    Each side gets a value_share = my_value / (my_value + their_value).
    Grade derived from that share (A+ → F-).  Trades with no resolvable stats
    are marked grade='?' (pending/ungraded).
    """
    conn = get_connection()

    # Pull all trades with their two sides and all assets
    where = "AND s.year = ?" if year else ""
    params = (year,) if year else ()

    trades_raw = conn.execute(f"""
        SELECT
            t.id            AS trade_id,
            s.year          AS trade_year,
            t.week          AS trade_week,
            t.transaction_date,
            ta.id           AS asset_id,
            ta.asset_type,
            ta.player_id,
            ta.sending_team_id,
            ta.receiving_team_id,
            ta.pick_season_year,
            ta.pick_round,
            p.full_name     AS player_name,
            p.position,
            -- sending manager
            send_mgr.display_name  AS sender,
            -- receiving manager
            recv_mgr.display_name  AS receiver
        FROM trades t
        JOIN seasons s ON s.id = t.season_id
        JOIN trade_assets ta ON ta.trade_id = t.id
        -- sending side manager
        JOIN teams send_team  ON send_team.id  = ta.sending_team_id
        JOIN managers send_mgr ON send_mgr.id  = send_team.manager_id
        -- receiving side manager
        JOIN teams recv_team  ON recv_team.id  = ta.receiving_team_id
        JOIN managers recv_mgr ON recv_mgr.id  = recv_team.manager_id
        LEFT JOIN players p ON p.id = ta.player_id
        WHERE 1=1 {where}
        ORDER BY s.year, t.week, t.id
    """, params).fetchall()

    # Compute post-trade fantasy points for every player in the DB (across all seasons)
    # keyed by (player_id, season_year) → total fantasy_points
    player_season_pts = {}
    for row in conn.execute("""
        SELECT pws.player_id, s.year, SUM(pws.fantasy_points) AS pts
        FROM player_weekly_stats pws
        JOIN seasons s ON s.id = pws.season_id
        GROUP BY pws.player_id, s.year
    """):
        player_season_pts[(row["player_id"], row["year"])] = row["pts"] or 0.0

    # For draft picks: resolve which player was actually drafted by the *receiving* manager
    # in pick_season_year at pick_round (non-keeper pick).
    # We match: seasons.year = pick_season_year, teams.manager_id = receiving manager,
    # draft_picks.round = pick_round, is_keeper = 0
    # Returns player_id of the drafted player.
    pick_player_cache: dict = {}

    def resolve_pick_player(recv_mgr_name: str, pick_year: int, pick_round: int):
        key = (recv_mgr_name, pick_year, pick_round)
        if key in pick_player_cache:
            return pick_player_cache[key]
        row = conn.execute("""
            SELECT dp.player_id
            FROM draft_picks dp
            JOIN teams t ON t.id = dp.team_id
            JOIN managers m ON m.id = t.manager_id
            JOIN seasons s ON s.id = dp.season_id AND s.year = ?
            WHERE m.display_name = ?
              AND dp.round = ?
              AND dp.is_keeper = 0
            LIMIT 1
        """, (pick_year, recv_mgr_name, pick_round)).fetchone()
        result = row["player_id"] if row else None
        pick_player_cache[key] = result
        return result

    def player_value(player_id: int, from_year: int) -> float:
        """Sum fantasy points for player across all seasons >= from_year."""
        return sum(
            v for (pid, yr), v in player_season_pts.items()
            if pid == player_id and yr >= from_year
        )

    # Aggregate by trade → manager → value_received
    # Structure: { trade_id: { manager_name: { value, assets[] } } }
    trade_map: dict = {}
    trade_meta: dict = {}

    for row in trades_raw:
        tid = row["trade_id"]
        if tid not in trade_map:
            trade_map[tid] = {}
            trade_meta[tid] = {
                "trade_id": tid,
                "year": row["trade_year"],
                "week": row["trade_week"],
                "transaction_date": row["transaction_date"],
            }

        receiver = row["receiver"]
        if receiver not in trade_map[tid]:
            trade_map[tid][receiver] = {"value": 0.0, "assets": []}

        asset_value = 0.0
        resolved_player = None
        asset_desc = ""

        if row["asset_type"] == "player" and row["player_id"]:
            asset_value = player_value(row["player_id"], row["trade_year"])
            resolved_player = row["player_name"]
            asset_desc = row["player_name"] or "Unknown"
        elif row["asset_type"] == "draft_pick":
            pick_pid = resolve_pick_player(
                receiver, row["pick_season_year"], row["pick_round"]
            )
            if pick_pid:
                asset_value = player_value(pick_pid, row["pick_season_year"])
                # look up name
                p_row = conn.execute(
                    "SELECT full_name FROM players WHERE id = ?", (pick_pid,)
                ).fetchone()
                resolved_player = p_row["full_name"] if p_row else None
            asset_desc = f"Pick {row['pick_season_year']} Rd {row['pick_round']}"
            if resolved_player:
                asset_desc += f" → {resolved_player}"

        trade_map[tid][receiver]["value"] += asset_value
        trade_map[tid][receiver]["assets"].append({
            "asset_type": row["asset_type"],
            "description": asset_desc,
            "position": row["position"],
            "player_name": row["player_name"],
            "resolved_player": resolved_player,
            "fantasy_points": round(asset_value, 1),
        })

    # Build output
    results = []
    for tid, sides in trade_map.items():
        meta = trade_meta[tid]
        managers = list(sides.keys())
        total_value = sum(s["value"] for s in sides.values())

        sides_out = []
        for mgr, data in sides.items():
            share = (data["value"] / total_value) if total_value > 0 else 0.5
            grade = _grade_share(share) if total_value > 0 else "?"
            label = _grade_label(share) if total_value > 0 else "Pending"
            sides_out.append({
                "manager": mgr,
                "value_received": round(data["value"], 1),
                "value_share": round(share, 3),
                "grade": grade,
                "grade_label": label,
                "assets": data["assets"],
            })

        # Sort: winner first
        sides_out.sort(key=lambda s: s["value_received"], reverse=True)

        results.append({
            **meta,
            "total_value": round(total_value, 1),
            "graded": total_value > 0,
            "sides": sides_out,
        })

    conn.close()
    results.sort(key=lambda r: (r["year"], r["week"] or 0, r["trade_id"]))
    return results


@router.get("/managers/{manager_name}")
def trades_by_manager(manager_name: str):
    """All trades a manager was involved in, as sender or receiver."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT *
        FROM v_trade_detail
        WHERE LOWER(sender) = LOWER(?) OR LOWER(receiver) = LOWER(?)
        ORDER BY year, trade_week, trade_id
    """, (manager_name, manager_name)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.get("/{trade_id}")
def get_trade(trade_id: int):
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM v_trade_detail WHERE trade_id = ?
    """, (trade_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]
