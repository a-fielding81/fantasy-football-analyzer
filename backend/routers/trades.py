from typing import Optional
from fastapi import APIRouter, Query
from db.database import get_connection

router = APIRouter()


# ---------------------------------------------------------------------------
# Grading helpers
# ---------------------------------------------------------------------------

def _grade_share(share: float) -> str:
    """Convert a side's value share (0–1) into a letter grade."""
    if share >= 0.68: return "A+"
    if share >= 0.62: return "A"
    if share >= 0.57: return "B+"
    if share >= 0.52: return "B"
    if share >= 0.47: return "C"   # roughly even
    if share >= 0.42: return "D"
    if share >= 0.35: return "F"
    return "F-"


def _grade_label(share: float) -> str:
    if share >= 0.62: return "Won"
    if share >= 0.47: return "Even"
    return "Lost"


# ---------------------------------------------------------------------------
# Outcome value helpers  (actual post-trade fantasy points)
# ---------------------------------------------------------------------------

def _build_player_season_pts(conn) -> dict:
    pts = {}
    for row in conn.execute("""
        SELECT pws.player_id, s.year, SUM(pws.fantasy_points) AS total
        FROM player_weekly_stats pws
        JOIN seasons s ON s.id = pws.season_id
        GROUP BY pws.player_id, s.year
    """):
        pts[(row["player_id"], row["year"])] = row["total"] or 0.0
    return pts


def _player_outcome(player_id: int, from_year: int, pts_map: dict,
                    to_year: Optional[int] = None) -> float:
    """Sum fantasy points from from_year through to_year (inclusive).
    Phase 2: default to a 2-season window (from_year + 1) so process and
    outcome are on the same time horizon."""
    if to_year is None:
        to_year = from_year + 1
    return sum(v for (pid, yr), v in pts_map.items()
               if pid == player_id and from_year <= yr <= to_year)


def _resolve_pick_player(recv_mgr: str, pick_year: int, pick_round: int,
                          cache: dict, conn) -> Optional[int]:
    key = (recv_mgr, pick_year, pick_round)
    if key in cache:
        return cache[key]
    row = conn.execute("""
        SELECT dp.player_id
        FROM draft_picks dp
        JOIN teams t ON t.id = dp.team_id
        JOIN managers m ON m.id = t.manager_id
        JOIN seasons s ON s.id = dp.season_id AND s.year = ?
        WHERE m.display_name = ? AND dp.round = ? AND dp.is_keeper = 0
        LIMIT 1
    """, (pick_year, recv_mgr, pick_round)).fetchone()
    result = row["player_id"] if row else None
    cache[key] = result
    return result


# ---------------------------------------------------------------------------
# Times-kept lookup
# ---------------------------------------------------------------------------

def _times_kept_before(player_id: int, manager_id: int, before_year: int,
                        conn) -> int:
    row = conn.execute("""
        SELECT COUNT(*) AS n
        FROM draft_picks dp
        JOIN teams t  ON t.id  = dp.team_id
        JOIN seasons s ON s.id = dp.season_id
        WHERE dp.player_id    = ?
          AND t.manager_id    = ?
          AND dp.is_keeper    = 1
          AND s.year          < ?
    """, (player_id, manager_id, before_year)).fetchone()
    return row["n"] if row else 0


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/")
def list_trades(year: Optional[int] = Query(None)):
    conn = get_connection()
    where  = "WHERE s.year = ?" if year else ""
    params = (year,) if year else ()
    rows = conn.execute(f"""
        SELECT DISTINCT t.id AS trade_id, s.year, t.week,
               t.transaction_date, t.status
        FROM trades t
        JOIN seasons s ON s.id = t.season_id
        {where}
        ORDER BY s.year, t.week, t.id
    """, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.get("/detail")
def trade_detail(year: Optional[int] = Query(None)):
    conn = get_connection()
    where  = "WHERE year = ?" if year else ""
    params = (year,) if year else ()
    rows = conn.execute(
        f"SELECT * FROM v_trade_detail {where} ORDER BY year, trade_week, trade_id, asset_type",
        params
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.get("/grades")
def trade_grades(year: Optional[int] = Query(None)):
    """
    Grade every trade with two independent assessments:

    PROCESS GRADE  — what the trade looked like at decision time.
      Uses ML model (Random Forest) trained on 26 seasons of NFL data.
      For players: predicts 2-season PPR at time of trade, keeper-weighted.
      For picks: historical round-average value.

    OUTCOME GRADE  — what actually happened.
      Sums actual fantasy points accumulated post-trade (current system).

    Each side gets a value_share for each grade independently.
    """
    conn = get_connection()

    # Lazy-load ML valuator (models loaded once per process)
    try:
        from ml.trade_valuation import get_valuator
        valuator = get_valuator()
        ml_available = True
    except Exception as e:
        valuator = None
        ml_available = False

    # Pull all trade assets with manager/player metadata
    where  = "AND s.year = ?" if year else ""
    params = (year,) if year else ()

    assets_raw = conn.execute(f"""
        SELECT
            t.id                   AS trade_id,
            s.year                 AS trade_year,
            t.week                 AS trade_week,
            t.transaction_date,
            ta.id                  AS asset_id,
            ta.asset_type,
            ta.player_id,
            ta.sending_team_id,
            ta.receiving_team_id,
            ta.pick_season_year,
            ta.pick_round,
            p.full_name            AS player_name,
            p.position,
            send_mgr.id            AS sender_mgr_id,
            send_mgr.display_name  AS sender,
            recv_mgr.id            AS recv_mgr_id,
            recv_mgr.display_name  AS receiver
        FROM trades t
        JOIN seasons s          ON s.id   = t.season_id
        JOIN trade_assets ta    ON ta.trade_id = t.id
        JOIN teams send_team    ON send_team.id  = ta.sending_team_id
        JOIN managers send_mgr  ON send_mgr.id   = send_team.manager_id
        JOIN teams recv_team    ON recv_team.id   = ta.receiving_team_id
        JOIN managers recv_mgr  ON recv_mgr.id    = recv_team.manager_id
        LEFT JOIN players p     ON p.id = ta.player_id
        WHERE 1=1 {where}
        ORDER BY s.year, t.week, t.id
    """, params).fetchall()

    # Pre-build outcome data
    pts_map    = _build_player_season_pts(conn)
    pick_cache = {}

    # ── Aggregate by trade → manager ─────────────────────────────────────────
    # Structure: trade_id → manager_name → {process_value, outcome_value, assets[]}
    trade_map  = {}
    trade_meta = {}

    for row in assets_raw:
        tid = row["trade_id"]
        if tid not in trade_map:
            trade_map[tid]  = {}
            trade_meta[tid] = {
                "trade_id":        tid,
                "year":            row["trade_year"],
                "week":            row["trade_week"],
                "transaction_date":row["transaction_date"],
            }

        receiver     = row["receiver"]
        recv_mgr_id  = row["recv_mgr_id"]
        trade_year   = row["trade_year"]

        if receiver not in trade_map[tid]:
            trade_map[tid][receiver] = {
                "process_value": 0.0,
                "outcome_value": 0.0,
                "assets":        [],
                "mgr_id":        recv_mgr_id,
            }

        # ── Asset values ────────────────────────────────────────────────────
        asset_info = {
            "asset_type":      row["asset_type"],
            "player_name":     row["player_name"],
            "position":        row["position"],
            "description":     "",
            # Outcome
            "outcome_points":  0.0,
            # Process
            "process_value":   0.0,
            "predicted_2yr":   0.0,
            "keeper_prob":     None,
            "key_factors":     {},
            "data_year":       None,
            "missing_data":    True,
            "is_rookie_proj":  False,
            "low_confidence":  False,
        }

        if row["asset_type"] == "player" and row["player_id"]:
            pid = row["player_id"]

            # Outcome
            outcome_pts = _player_outcome(pid, trade_year, pts_map)
            asset_info["outcome_points"] = round(outcome_pts, 1)
            asset_info["description"]    = row["player_name"] or "Unknown"

            # Process (ML)
            if ml_available:
                tkb = _times_kept_before(pid, recv_mgr_id, trade_year, conn)
                val = valuator.value_player(pid, trade_year, conn,
                                            times_kept_before=tkb)
                asset_info.update({
                    "process_value":  val["process_value"],
                    "predicted_2yr":  val["predicted_2yr"],
                    "keeper_prob":    val["keeper_prob"],
                    "key_factors":    val["key_factors"],
                    "data_year":      val["data_year"],
                    "missing_data":   val["missing_data"],
                    "is_rookie_proj": val.get("is_rookie_proj", False),
                    "low_confidence": val.get("low_confidence", False),
                })

        elif row["asset_type"] == "draft_pick":
            pick_year  = row["pick_season_year"]
            pick_round = row["pick_round"]
            desc = f"Pick {pick_year} Rd {pick_round}"

            # Outcome: resolve which player was actually drafted
            pick_pid = _resolve_pick_player(
                receiver, pick_year, pick_round, pick_cache, conn
            )
            if pick_pid:
                outcome_pts = _player_outcome(pick_pid, pick_year, pts_map)
                p_row = conn.execute(
                    "SELECT full_name FROM players WHERE id = ?", (pick_pid,)
                ).fetchone()
                resolved = p_row["full_name"] if p_row else "?"
                desc += f" → {resolved}"
                asset_info["outcome_points"] = round(outcome_pts, 1)

            asset_info["description"] = desc

            # Process: historical round average with keeper discount + future discount
            if ml_available and pick_round:
                val = valuator.value_pick(
                    pick_round, pick_year or trade_year, trade_year=trade_year
                )
                asset_info.update({
                    "process_value":  val["process_value"],
                    "predicted_2yr":  val["round_avg_ppr"],
                    "keeper_prob":    val["keeper_prob"],
                    "key_factors":    val["key_factors"],
                    "missing_data":   False,
                    "is_rookie_proj": False,
                    "low_confidence": val.get("low_confidence", False),
                })

        trade_map[tid][receiver]["process_value"] += asset_info["process_value"]
        trade_map[tid][receiver]["outcome_value"] += asset_info["outcome_points"]
        trade_map[tid][receiver]["assets"].append(asset_info)

    # ── Build output ─────────────────────────────────────────────────────────
    results = []
    for tid, sides in trade_map.items():
        meta = trade_meta[tid]
        mgrs = list(sides.keys())

        total_process = sum(s["process_value"] for s in sides.values())
        total_outcome = sum(s["outcome_value"] for s in sides.values())

        sides_out = []
        for mgr, data in sides.items():
            # Process grade
            if total_process > 0:
                p_share = data["process_value"] / total_process
                process_grade = _grade_share(p_share)
                process_label = _grade_label(p_share)
            else:
                p_share = 0.5
                process_grade = "?"
                process_label = "Pending"

            # Outcome grade
            if total_outcome > 0:
                o_share = data["outcome_value"] / total_outcome
                outcome_grade = _grade_share(o_share)
                outcome_label = _grade_label(o_share)
            else:
                o_share = 0.5
                outcome_grade = "?"
                outcome_label = "Pending"

            # Phase 3: flag sides where most assets are uncertain
            n_assets = len(data["assets"])
            n_low_conf = sum(1 for a in data["assets"] if a.get("low_confidence"))
            side_low_conf = n_assets > 0 and (n_low_conf / n_assets) >= 0.5

            sides_out.append({
                "manager":        mgr,
                # Process
                "process_value":  round(data["process_value"], 1),
                "process_share":  round(p_share, 3),
                "process_grade":  process_grade,
                "process_label":  process_label,
                # Outcome
                "outcome_value":  round(data["outcome_value"], 1),
                "outcome_share":  round(o_share, 3),
                "outcome_grade":  outcome_grade,
                "outcome_label":  outcome_label,
                # Confidence
                "low_confidence": side_low_conf,
                # Assets
                "assets":         data["assets"],
            })

        # Sort by process value (winner first)
        sides_out.sort(key=lambda s: s["process_value"], reverse=True)

        trade_low_conf = any(s["low_confidence"] for s in sides_out)
        results.append({
            **meta,
            "ml_graded":      ml_available and total_process > 0,
            "outcome_graded": total_outcome > 0,
            "low_confidence": trade_low_conf,
            "total_process":  round(total_process, 1),
            "total_outcome":  round(total_outcome, 1),
            "sides":          sides_out,
        })

    conn.close()
    results.sort(key=lambda r: (r["year"], r["week"] or 0, r["trade_id"]))
    return results


@router.get("/managers/{manager_name}")
def trades_by_manager(manager_name: str):
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM v_trade_detail
        WHERE LOWER(sender) = LOWER(?) OR LOWER(receiver) = LOWER(?)
        ORDER BY year, trade_week, trade_id
    """, (manager_name, manager_name)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.get("/{trade_id}")
def get_trade(trade_id: int):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM v_trade_detail WHERE trade_id = ?", (trade_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
