from fastapi import APIRouter, Query
from db.database import get_connection

router = APIRouter()


@router.get("/search")
def search_players(q: str = Query(..., min_length=2)):
    conn = get_connection()
    rows = conn.execute("""
        SELECT id, full_name, position, nfl_team, sleeper_id, espn_id
        FROM players
        WHERE full_name LIKE ?
        ORDER BY full_name
        LIMIT 20
    """, (f"%{q}%",)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.get("/{player_id}/history")
def player_history(player_id: int):
    """Season-by-season fantasy point totals for a player."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT
            s.year,
            s.platform,
            ROUND(SUM(pws.fantasy_points), 2)  AS total_fantasy_points,
            COUNT(pws.week)                     AS weeks_played,
            ROUND(AVG(pws.fantasy_points), 2)   AS avg_per_week,
            MAX(pws.fantasy_points)             AS best_week
        FROM player_weekly_stats pws
        JOIN seasons s ON s.id = pws.season_id
        WHERE pws.player_id = ?
        GROUP BY s.id
        ORDER BY s.year
    """, (player_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.get("/{player_id}/trades")
def player_trade_history(player_id: int):
    """All trades this player was involved in."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT
            s.year,
            tr.week,
            tr.transaction_date,
            send_m.display_name AS sent_by,
            recv_m.display_name AS received_by
        FROM trade_assets ta
        JOIN trades tr ON tr.id = ta.trade_id
        JOIN seasons s ON s.id = tr.season_id
        JOIN teams send_t ON send_t.id = ta.sending_team_id
        JOIN managers send_m ON send_m.id = send_t.manager_id
        JOIN teams recv_t ON recv_t.id = ta.receiving_team_id
        JOIN managers recv_m ON recv_m.id = recv_t.manager_id
        WHERE ta.player_id = ?
        ORDER BY s.year, tr.week
    """, (player_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]
