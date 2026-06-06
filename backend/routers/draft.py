from __future__ import annotations

from fastapi import APIRouter, Query
from db.database import get_connection

router = APIRouter()


@router.get("/")
def draft_summary(year: int | None = Query(None)):
    conn = get_connection()
    where = "WHERE year = ?" if year else ""
    params = (year,) if year else ()
    rows = conn.execute(f"""
        SELECT * FROM v_draft_pick_summary
        {where}
        ORDER BY year, pick_number
    """, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.get("/value-over-adp")
def value_over_adp(year: int | None = Query(None)):
    """
    For picks where we have ADP, compute fantasy_points vs. positional expectation.
    This is a foundational query for draft grading.
    """
    conn = get_connection()
    where = "AND d.year = ?" if year else ""
    params = (year,) if year else ()
    rows = conn.execute(f"""
        WITH adp_picks AS (
            SELECT
                d.year,
                d.manager,
                d.round,
                d.pick_number,
                d.player_name,
                d.position,
                d.adp_at_draft,
                d.is_keeper,
                d.season_fantasy_points,
                -- How many picks before/after ADP did they take this player?
                CASE
                    WHEN d.adp_at_draft IS NOT NULL
                    THEN ROUND(d.adp_at_draft - d.pick_number, 1)
                    ELSE NULL
                END AS picks_relative_to_adp
            FROM v_draft_pick_summary d
            WHERE d.player_name IS NOT NULL
              AND d.season_fantasy_points > 0
              {where}
        )
        SELECT
            *,
            -- Positive = reached; Negative = value / fell
            CASE
                WHEN picks_relative_to_adp > 5  THEN 'reach'
                WHEN picks_relative_to_adp < -5 THEN 'value'
                ELSE 'on_value'
            END AS adp_grade
        FROM adp_picks
        ORDER BY year, pick_number
    """, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.get("/keepers")
def keeper_history(year: int | None = Query(None)):
    """All keeper designations across seasons."""
    conn = get_connection()
    where = "AND year = ?" if year else ""
    params = (year,) if year else ()
    rows = conn.execute(f"""
        SELECT *
        FROM v_draft_pick_summary
        WHERE is_keeper = 1
        {where}
        ORDER BY year, pick_number
    """, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]
