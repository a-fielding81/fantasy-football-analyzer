from fastapi import APIRouter
from db.database import get_connection

router = APIRouter()


@router.get("/")
def list_managers():
    """All managers with their career record across all seasons."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT
            m.display_name,
            COUNT(DISTINCT t.season_id)      AS seasons_played,
            SUM(t.wins)                       AS total_wins,
            SUM(t.losses)                     AS total_losses,
            ROUND(SUM(t.points_for), 2)       AS total_pf,
            ROUND(AVG(t.points_for), 2)       AS avg_pf_per_season,
            MIN(t.final_rank)                 AS best_finish,
            MAX(s.year)                       AS last_season
        FROM managers m
        JOIN teams t ON t.manager_id = m.id
        JOIN seasons s ON s.id = t.season_id
        GROUP BY m.id
        ORDER BY total_wins DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.get("/{manager_name}/history")
def manager_history(manager_name: str):
    """Season-by-season breakdown for a specific manager."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT *
        FROM v_team_season_summary
        WHERE LOWER(manager) = LOWER(?)
    """, (manager_name,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]
