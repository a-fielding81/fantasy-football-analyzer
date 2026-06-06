from __future__ import annotations

from fastapi import APIRouter
from db.database import get_connection

router = APIRouter()


@router.get("/")
def list_seasons():
    conn = get_connection()
    rows = conn.execute("""
        SELECT
            s.id,
            s.year,
            s.platform,
            s.platform_season_id,
            l.name AS league_name,
            COUNT(t.id) AS team_count
        FROM seasons s
        JOIN leagues l ON l.id = s.league_id
        LEFT JOIN teams t ON t.season_id = s.id
        GROUP BY s.id
        ORDER BY s.year
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.get("/{year}/standings")
def standings(year: int):
    conn = get_connection()
    rows = conn.execute("""
        SELECT *
        FROM v_team_season_summary
        WHERE year = ?
    """, (year,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.get("/{year}/weekly-scores")
def weekly_scores(year: int):
    """Return all weekly scores for a season, shaped for chart consumption."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT
            ws.week,
            m.display_name AS manager,
            t.team_name,
            ws.points_scored,
            ws.points_against,
            ws.is_playoff
        FROM weekly_scores ws
        JOIN seasons s ON s.id = ws.season_id
        JOIN teams t ON t.id = ws.team_id
        JOIN managers m ON m.id = t.manager_id
        WHERE s.year = ?
        ORDER BY ws.week, m.display_name
    """, (year,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]
