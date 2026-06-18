"""
Trade valuation engine.

Given a player (or draft pick) and the season in which a trade occurred,
computes:
  - process_value  : forward-looking ML value AT TRADE TIME
  - key_factors    : dict of the most influential features for transparency

Phase 1A — Rookie valuation
  Players with no qualifying prior season (rookies / year-1 players) are no
  longer zeroed out. They fall back to a fantasy-ADP-based projection using
  their draft round in the same year, or a position-based median otherwise.

Phase 1B — Pick symmetry + monotonic curve
  Pick values are now:
    1. Refit from league data using isotonic regression (strict monotonic decay)
    2. Subject to the same keeper-weight discount as player values so there is
       no structural bias in favour of picks over players.
    3. Discounted 2 % per year for future picks (uncertainty, not opportunity cost).

Phase 2 — Outcome window
  Outcome now counts only seasons trade_year and trade_year+1 (2-season window)
  so process and outcome grades are on the same time horizon.

Phase 3 — Confidence flags
  Trades with mostly rookie projections or far-future picks are flagged as
  low-confidence, so the UI can render a ⚠ marker instead of a confident grade.
"""

from __future__ import annotations
import pickle
import numpy as np
from pathlib import Path
from typing import Optional

# ── Phase 1B: Monotonic pick-value table ────────────────────────────────────
# Re-computed from our league's non-keeper fantasy draft picks (N=43-57/round
# for Rd1-7). Isotonic regression enforces strict monotonicity for Rd1-7;
# Rd8-17 use a linear decay from Rd7→Rd17 (183) because sample sizes shrink and
# selection bias inflates raw late-round means.
ROUND_AVG_PPR: dict[int, float] = {
    1:  368.0,
    2:  363.0,
    3:  333.0,
    4:  333.0,
    5:  303.0,
    6:  303.0,
    7:  267.0,
    8:  259.0,
    9:  250.0,
    10: 242.0,
    11: 233.0,
    12: 225.0,
    13: 217.0,
    14: 208.0,
    15: 200.0,
    16: 192.0,
    17: 183.0,
}
_ROUND_FALLBACK = 175.0

# ── Phase 1B: Per-round keeper probability ───────────────────────────────────
# Smoothed empirical keep-rate from our league (fraction of Rd-N picks that
# were kept by the same manager the following season).
# Late-round data is noisy; values are smoothed monotonically downward.
ROUND_KEEPER_PROB: dict[int, float] = {
    1:  0.25,
    2:  0.22,
    3:  0.16,
    4:  0.14,
    5:  0.11,
    6:  0.10,
    7:  0.08,
    8:  0.07,
    9:  0.06,
    10: 0.06,
    11: 0.06,
    12: 0.06,
    13: 0.06,
    14: 0.06,
    15: 0.06,
    16: 0.06,
    17: 0.05,
}
_ROUND_KEEPER_FALLBACK = 0.05

# ── Phase 1A: Position-based rookie projection baselines ────────────────────
# Median 2-yr PPR for players in their first two qualifying seasons (1999-2022).
# Used when a player has no prior NFL data AND no fantasy draft information.
ROOKIE_MEDIAN_2YR: dict[str, float] = {
    "QB": 249.0,
    "RB": 156.0,
    "WR": 161.0,
    "TE": 106.0,
}
_ROOKIE_MEDIAN_FALLBACK = 140.0

# Default keeper probability for a pure rookie projection (no observed stats)
_ROOKIE_KEEPER_PROB = 0.18   # slightly above average for an unproven player

POS_MAP = {"QB": 0, "RB": 1, "WR": 2, "TE": 3}
SKILL_POSITIONS = {"QB", "RB", "WR", "TE"}   # positions the ML model understands

_MODEL_DIR = Path(__file__).parent


class TradeValuator:
    """Load ML models once, expose valuation helpers."""

    def __init__(self):
        prod_path   = _MODEL_DIR / "production_model.pkl"
        keeper_path = _MODEL_DIR / "keeper_model.pkl"

        with open(prod_path, "rb") as f:
            self._prod = pickle.load(f)
        with open(keeper_path, "rb") as f:
            self._keeper = pickle.load(f)

        self._prod_rf      = self._prod["model"]
        self._prod_feats   = self._prod["features"]
        self._keeper_pipe  = self._keeper["model"]
        self._keeper_feats = self._keeper["features"]

    # ── Public API ──────────────────────────────────────────────────────────

    def value_player(
        self,
        player_id: int,
        trade_year: int,
        conn,
        times_kept_before: int = 0,
    ) -> dict:
        """
        Compute forward-looking value for a player at trade time.

        Returns:
            {
              process_value      : float   (2-season PPR prediction × keep weight)
              predicted_2yr      : float   (raw RF prediction or rookie projection)
              keeper_prob        : float   (P(kept next season))
              keep_weight        : float   (value multiplier)
              key_factors        : dict    (top feature values for UI transparency)
              data_year          : int     (which season's stats were used, or None)
              missing_data       : bool    (True if truly no data found)
              is_rookie_proj     : bool    (True if using rookie projection fallback)
              low_confidence     : bool    (True when estimate is highly uncertain)
            }
        """
        feats, meta = self._get_player_features(player_id, trade_year, conn)

        # ── Non-skill positions (DEF, K, DST, UNKNOWN) ─────────────────────
        if meta.get("is_non_skill"):
            pos_label = meta.get("position") or "non-skill"
            return {
                "process_value":  0.0,
                "predicted_2yr":  0.0,
                "keeper_prob":    0.0,
                "keep_weight":    0.0,
                "key_factors":    {
                    "position": pos_label,
                    "note": f"{pos_label} — not ML-gradeable",
                },
                "data_year":      None,
                "missing_data":   True,
                "is_rookie_proj": False,
                "low_confidence": True,
                "is_non_skill":   True,
            }

        # ── Phase 1A: Rookie / no-prior-data path ───────────────────────────
        if feats is None:
            return self._value_rookie(player_id, trade_year, conn, meta)

        # ── Normal ML path ──────────────────────────────────────────────────
        X_prod = np.array([[feats.get(f, 0.0) for f in self._prod_feats]])
        pred_2yr = float(self._prod_rf.predict(X_prod)[0])
        pred_2yr = max(0.0, pred_2yr)

        # Look up injury context for the data year (same year the stats come from)
        data_year = meta.get("data_year") or (trade_year - 1)
        gsis_id   = meta.get("gsis_id")
        weeks_out     = 0
        injury_bucket = 0
        if gsis_id:
            inj_row = conn.execute("""
                SELECT weeks_out, injury_bucket
                FROM player_season_injuries
                WHERE gsis_id = ? AND season_year = ?
            """, (gsis_id, data_year)).fetchone()
            if inj_row:
                weeks_out     = inj_row["weeks_out"]     or 0
                injury_bucket = inj_row["injury_bucket"] or 0

        prior_games  = max(feats.get("games_t", 1), 1)
        ppr_per_game = feats.get("pts_t", 0) / prior_games

        keeper_feats = {
            "pos_enc":            feats.get("pos_enc", 2),
            "age":                feats.get("age", 26),
            "ppr_per_game":       ppr_per_game,
            "prior_games":        prior_games,
            "prior_target_share": feats.get("target_share_t", 0),
            "prior_carries":      feats.get("carries_t", 0),
            "prior_wopr":         feats.get("wopr_t", 0),
            "times_kept_before":  times_kept_before,
            "weeks_out":          weeks_out,
            "injury_bucket":      injury_bucket,
        }
        X_keep = np.array([[keeper_feats.get(f, 0.0) for f in self._keeper_feats]])
        keeper_prob = float(self._keeper_pipe.predict_proba(X_keep)[0, 1])

        keep_weight   = 0.5 * (1.0 + keeper_prob)
        process_value = pred_2yr * keep_weight

        key_factors = {
            "prior_ppr":           round(feats.get("pts_t", 0), 1),
            "ppr_per_game":        round(ppr_per_game, 1),
            "weeks_out":           weeks_out,
            "injury_bucket":       injury_bucket,
            "age":                 feats.get("age"),
            "position":            meta.get("position"),
            "team":                meta.get("team"),
            "target_share":        round(feats.get("target_share_t", 0), 3),
            "carries":             feats.get("carries_t"),
            "wopr":                round(feats.get("wopr_t", 0), 3),
            "team_pass_rate":      round(feats.get("team_pass_rate_t", 0), 3),
            "team_11_rate":        round(feats.get("team_11_rate_t", 0), 3),
            "hc_tenure":           feats.get("team_hc_tenure_t"),
            "new_oc":              bool(feats.get("team_new_oc_t", 0)),
            "hc_midseason_change": bool(feats.get("team_hc_midseason_t", 0)),
            "coaching_tree":       meta.get("coaching_tree"),
        }

        return {
            "process_value":  round(process_value, 1),
            "predicted_2yr":  round(pred_2yr, 1),
            "keeper_prob":    round(keeper_prob, 3),
            "keep_weight":    round(keep_weight, 3),
            "key_factors":    key_factors,
            "data_year":      meta.get("data_year"),
            "missing_data":   False,
            "is_rookie_proj": False,
            "low_confidence": False,
        }

    def value_pick(self, pick_round: int, pick_year: int,
                   trade_year: Optional[int] = None) -> dict:
        """
        Value a draft pick.

        Phase 1B changes:
          - Uses refit monotonic round-average curve.
          - Applies the same keeper-weight discount as player values
            (eliminating the structural pick > player bias).
          - Applies a 2 % future-year discount for picks in seasons after
            trade_year.
        """
        base = ROUND_AVG_PPR.get(pick_round, _ROUND_FALLBACK)

        # Keeper discount — same formula as value_player
        keeper_prob = ROUND_KEEPER_PROB.get(pick_round, _ROUND_KEEPER_FALLBACK)
        keep_weight = 0.5 * (1.0 + keeper_prob)

        # Future-pick discount (2 % per year of wait)
        years_out = 0
        if trade_year and pick_year and pick_year > trade_year:
            years_out = pick_year - trade_year
        future_discount = (0.98 ** years_out)

        process_value = base * keep_weight * future_discount

        note = f"Rd{pick_round} avg {base:.0f} × keep-wt {keep_weight:.2f}"
        if years_out:
            note += f" × {future_discount:.2f} future disc ({years_out}yr)"

        return {
            "process_value":  round(process_value, 1),
            "round_avg_ppr":  round(base, 1),
            "pick_round":     pick_round,
            "pick_year":      pick_year,
            "keeper_prob":    round(keeper_prob, 3),
            "keep_weight":    round(keep_weight, 3),
            "years_out":      years_out,
            "key_factors": {
                "round":       pick_round,
                "avg_ppr":     round(base, 1),
                "keep_weight": round(keep_weight, 3),
                "years_out":   years_out,
                "note":        note,
            },
            "missing_data":   False,
            "is_rookie_proj": False,
            "low_confidence": years_out >= 2,
        }

    # ── Internal helpers ────────────────────────────────────────────────────

    def _value_rookie(
        self, player_id: int, trade_year: int, conn, meta: dict
    ) -> dict:
        """
        Phase 1A: value a player with no qualifying prior NFL season.

        Priority order:
          1. Fantasy draft round in trade_year  → use ROUND_AVG_PPR as proxy
             (reflects community consensus on the player's upside at trade time)
          2. Position-based rookie median (ROOKIE_MEDIAN_2YR)
          3. Generic fallback (unknown position / kickers / DST)
        """
        pos = meta.get("position") or "WR"

        # Look for their fantasy draft round in the trade year
        pick_row = conn.execute("""
            SELECT dp.round, dp.pick_number
            FROM draft_picks dp
            JOIN seasons s ON s.id = dp.season_id
            WHERE dp.player_id = ? AND s.year = ? AND dp.is_keeper = 0
            ORDER BY dp.pick_number
            LIMIT 1
        """, (player_id, trade_year)).fetchone()

        if pick_row:
            draft_round = pick_row["round"]
            pred_2yr = ROUND_AVG_PPR.get(draft_round, _ROUND_FALLBACK)
            proj_basis = f"fantasy Rd{draft_round} ADP"
        elif pos in ROOKIE_MEDIAN_2YR:
            draft_round = None
            pred_2yr = ROOKIE_MEDIAN_2YR[pos]
            proj_basis = f"{pos} rookie median"
        else:
            draft_round = None
            pred_2yr = _ROOKIE_MEDIAN_FALLBACK
            proj_basis = "generic rookie baseline"

        keeper_prob   = _ROOKIE_KEEPER_PROB
        keep_weight   = 0.5 * (1.0 + keeper_prob)
        process_value = pred_2yr * keep_weight

        return {
            "process_value":  round(process_value, 1),
            "predicted_2yr":  round(pred_2yr, 1),
            "keeper_prob":    keeper_prob,
            "keep_weight":    round(keep_weight, 3),
            "key_factors": {
                "position":    pos,
                "proj_basis":  proj_basis,
                "draft_round": draft_round,
                "note":        "Rookie projection — no prior NFL season data",
            },
            "data_year":      None,
            "missing_data":   False,
            "is_rookie_proj": True,
            "low_confidence": True,
        }

    def _get_player_features(
        self, player_id: int, trade_year: int, conn
    ) -> tuple[Optional[dict], dict]:
        """
        Build the production-model feature vector for a player.

        Looks for qualifying stats at trade_year-1 (primary), trade_year-2
        (secondary), and trade_year itself (for mid-season trades of players
        with partial current-year data).

        Returns (features_dict, metadata_dict).
        features_dict is None when no usable data exists; metadata_dict always
        contains at least {position, is_rookie: True}.
        """
        player = conn.execute(
            "SELECT gsis_id, full_name, position, birth_date FROM players WHERE id = ?",
            (player_id,)
        ).fetchone()

        if not player:
            return None, {"is_non_skill": True, "position": None}

        pos = player["position"] or ""

        # Non-skill positions (DEF, K, DST, UNKNOWN, etc.) cannot be ML-graded
        if pos not in SKILL_POSITIONS:
            return None, {"is_non_skill": True, "position": pos}

        if not player["gsis_id"]:
            return None, {"position": pos, "is_rookie": True}

        gsis = player["gsis_id"]
        bd   = player["birth_date"] or ""

        # Search in priority order: prior year → two years ago → current year
        stats_yr = None
        for look_year in [trade_year - 1, trade_year - 2, trade_year]:
            row = conn.execute("""
                SELECT * FROM nfl_player_seasons
                WHERE gsis_id = ? AND season_year = ?
            """, (gsis, look_year)).fetchone()
            if row:
                stats_yr = row
                break

        if not stats_yr:
            return None, {"position": pos, "is_rookie": True}

        data_year = stats_yr["season_year"]

        prior_row = conn.execute("""
            SELECT fantasy_points_ppr, games FROM nfl_player_seasons
            WHERE gsis_id = ? AND season_year = ?
        """, (gsis, data_year - 1)).fetchone()

        pts_t          = stats_yr["fantasy_points_ppr"] or 0
        games_t        = stats_yr["games"] or 0
        pts_t_minus1   = (prior_row["fantasy_points_ppr"] or 0) if prior_row else pts_t
        games_t_minus1 = (prior_row["games"] or 0) if prior_row else games_t

        try:
            birth_year = int(bd[:4])
            age = trade_year - birth_year
        except (ValueError, TypeError, IndexError):
            age = 27

        team = stats_yr["team"] or ""
        scheme = conn.execute("""
            SELECT pass_rate, rate_11, rate_shotgun, hc_tenure,
                   new_oc, hc_midseason_change, coaching_tree
            FROM team_season_scheme
            WHERE season_year = ? AND team = ?
        """, (data_year, team)).fetchone()

        def _s(field, default=0.0):
            if scheme is None: return default
            v = scheme[field]
            return v if v is not None else default

        coaching_tree = _s("coaching_tree", "other")
        feats = {
            "pos_enc":                POS_MAP.get(pos, 2),
            "age":                    age,
            "pts_t":                  pts_t,
            "pts_t_minus1":           pts_t_minus1,
            "pts_trend":              pts_t - pts_t_minus1,
            "games_t":                games_t,
            "games_t_minus1":         games_t_minus1,
            "games_trend":            games_t - games_t_minus1,
            "carries_t":              stats_yr["carries"] or 0,
            "targets_t":              stats_yr["targets"] or 0,
            "target_share_t":         stats_yr["target_share"] or 0,
            "air_yards_share_t":      stats_yr["air_yards_share"] or 0,
            "wopr_t":                 stats_yr["wopr"] or 0,
            "rushing_epa_t":          stats_yr["rushing_epa"] or 0,
            "receiving_epa_t":        stats_yr["receiving_epa"] or 0,
            "team_pass_rate_t":       _s("pass_rate"),
            "team_pass_rate_change_t":0.0,
            "team_11_rate_t":         _s("rate_11"),
            "team_shotgun_rate_t":    _s("rate_shotgun"),
            "team_hc_tenure_t":       _s("hc_tenure"),
            "team_new_oc_t":          _s("new_oc"),
            "team_hc_midseason_t":    _s("hc_midseason_change"),
            "is_shanahan_mcvay":      int(coaching_tree == "shanahan_mcvay"),
            "is_belichick":           int(coaching_tree == "belichick"),
            "is_reid_wco":            int(coaching_tree == "reid_wco"),
        }

        meta = {
            "position":     pos,
            "team":         team,
            "data_year":    data_year,
            "gsis_id":      gsis,
            "coaching_tree":coaching_tree,
            "is_rookie":    False,
        }

        return feats, meta


# ── Singleton ────────────────────────────────────────────────────────────────

_valuator: Optional[TradeValuator] = None

def get_valuator() -> TradeValuator:
    global _valuator
    if _valuator is None:
        _valuator = TradeValuator()
    return _valuator
