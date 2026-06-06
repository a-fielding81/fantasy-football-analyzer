from __future__ import annotations

from fastapi import APIRouter, Query
from db.database import get_connection

router = APIRouter()


@router.get("/")
def list_trades(year: int | None = Query(None)):
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
def trade_detail(year: int | None = Query(None)):
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


@router.get("/{trade_id}")
def get_trade(trade_id: int):
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM v_trade_detail WHERE trade_id = ?
    """, (trade_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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
