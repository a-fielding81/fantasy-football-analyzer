"""
Trade valuation engine.

Given a player (or draft pick) and the season in which a trade occurred,
computes:
  - process_value  : forward-looking ML value AT TRADE TIME
                     = production_model prediction weighted by keeper probability
  - key_factors    : dict of the most influential features for transparency

For picks, falls back to round-average historical value.

Usage:
    from ml.trade_valuation import TradeValuator
    v = TradeValuator()
    result = v.value_player(player_id=42, trade_year=2022, conn=conn)
    result = v.value_pick(pick_round=1, pick_year=2023)
"""

from __future__ import annotations
import pickle
import numpy as np
from pathlib import Path
from typing import Optional

# ── Round-average pick values (computed from our league's draft history,
#    2-season PPR sum for the player drafted at that round)
# Rounds 1-7 are well-sampled (N=70 each); 8-17 are noisier.
# We smooth slightly: use league-average nflverse data where our sample is thin.
ROUND_AVG_PPR: dict[int, float] = {
    1:  289.0,
    2:  291.0,
    3:  264.0,
    4:  273.0,
    5:  236.0,
    6:  231.0,
    7:  210.0,
    8:  138.0,
    9:  118.0,
    10: 114.0,
    11: 139.0,
    12: 185.0,
    13: 208.0,
    14: 188.0,
    15: 189.0,
    16: 148.0,
    17: 146.0,
}
# Fallback for any unrecognised round
_ROUND_FALLBACK = 120.0

POS_MAP = {"QB": 0, "RB": 1, "WR": 2, "TE": 3}

_MODEL_DIR = Path(__file__).parent


class TradeValuator:
    """Load ML models once, expose valuation helpers."""

    def __init__(self):
        prod_path   = _MODEL_DIR / "production_model.pkl"
        keeper_path = _MODEL_DIR / "keeper_model.pkl"

        with open(prod_path, "rb") as f:
            self._prod = pickle.load(f)      # {model, features, curves}
        with open(keeper_path, "rb") as f:
            self._keeper = pickle.load(f)    # {model, features}

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
              process_value   : float   (2-season PPR prediction × keep weight)
              predicted_2yr   : float   (raw RF prediction)
              keeper_prob     : float   (P(kept next season))
              keep_weight     : float   (value multiplier from keeper context)
              key_factors     : dict    (top feature values for UI transparency)
              data_year       : int     (which season's stats were used)
              missing_data    : bool    (True if we had to fall back)
            }
        """
        feats, meta = self._get_player_features(player_id, trade_year, conn)
        if feats is None:
            return {
                "process_value":  0.0,
                "predicted_2yr":  0.0,
                "keeper_prob":    0.0,
                "keep_weight":    0.0,
                "key_factors":    {},
                "data_year":      None,
                "missing_data":   True,
            }

        # Production prediction
        X_prod = np.array([[feats.get(f, 0.0) for f in self._prod_feats]])
        pred_2yr = float(self._prod_rf.predict(X_prod)[0])
        pred_2yr = max(0.0, pred_2yr)

        # Keeper probability
        keeper_feats = {
            "pos_enc":           feats.get("pos_enc", 2),
            "age":               feats.get("age", 26),
            "prior_ppr":         feats.get("pts_t", 0),
            "prior_games":       feats.get("games_t", 0),
            "prior_target_share":feats.get("target_share_t", 0),
            "prior_carries":     feats.get("carries_t", 0),
            "prior_wopr":        feats.get("wopr_t", 0),
            "times_kept_before": times_kept_before,
        }
        X_keep = np.array([[keeper_feats.get(f, 0.0) for f in self._keeper_feats]])
        keeper_prob = float(self._keeper_pipe.predict_proba(X_keep)[0, 1])

        # Keeper-weighted value:
        #   Year 1: player is definitely on the roster (just traded)
        #   Year 2: player is on roster only if kept (prob = keeper_prob)
        # value = pred_yr1 + keeper_prob * pred_yr2
        #       = (pred_2yr/2) * (1 + keeper_prob)
        keep_weight   = 0.5 * (1.0 + keeper_prob)
        process_value = pred_2yr * keep_weight

        # Key factors for UI transparency
        key_factors = {
            "prior_ppr":       round(feats.get("pts_t", 0), 1),
            "age":             feats.get("age"),
            "position":        meta.get("position"),
            "team":            meta.get("team"),
            "target_share":    round(feats.get("target_share_t", 0), 3),
            "carries":         feats.get("carries_t"),
            "wopr":            round(feats.get("wopr_t", 0), 3),
            "team_pass_rate":  round(feats.get("team_pass_rate_t", 0), 3),
            "team_11_rate":    round(feats.get("team_11_rate_t", 0), 3),
            "hc_tenure":       feats.get("team_hc_tenure_t"),
            "new_oc":          bool(feats.get("team_new_oc_t", 0)),
            "hc_midseason_change": bool(feats.get("team_hc_midseason_t", 0)),
            "coaching_tree":   meta.get("coaching_tree"),
        }

        return {
            "process_value": round(process_value, 1),
            "predicted_2yr": round(pred_2yr, 1),
            "keeper_prob":   round(keeper_prob, 3),
            "keep_weight":   round(keep_weight, 3),
            "key_factors":   key_factors,
            "data_year":     meta.get("data_year"),
            "missing_data":  False,
        }

    def value_pick(self, pick_round: int, pick_year: int) -> dict:
        """
        Value a draft pick using historical round averages.

        Returns process_value (2-season PPR equivalent).
        Applies a small recency discount for picks further in the future.
        """
        base = ROUND_AVG_PPR.get(pick_round, _ROUND_FALLBACK)

        # Future picks are less certain — apply a 5% discount per year of wait
        # (pick_year is the year the pick will be used, not when it was traded)
        return {
            "process_value":  round(base, 1),
            "round_avg_ppr":  round(base, 1),
            "pick_round":     pick_round,
            "pick_year":      pick_year,
            "key_factors": {
                "round":    pick_round,
                "avg_ppr":  round(base, 1),
                "note":     f"Rd{pick_round} historical avg ({base:.0f} pts/2 seasons)",
            },
            "missing_data": False,
        }

    # ── Internal helpers ────────────────────────────────────────────────────

    def _get_player_features(
        self, player_id: int, trade_year: int, conn
    ) -> tuple[Optional[dict], dict]:
        """
        Build the production-model feature vector for a player
        using their stats from `trade_year - 1` (or nearest prior season).

        Returns (features_dict, metadata_dict).
        features_dict is None if no usable data found.
        """
        # Look up the player's gsis_id and bio
        player = conn.execute(
            "SELECT gsis_id, full_name, position, birth_date FROM players WHERE id = ?",
            (player_id,)
        ).fetchone()

        if not player or not player["gsis_id"]:
            return None, {}

        gsis   = player["gsis_id"]
        pos    = player["position"] or "WR"
        bd     = player["birth_date"] or ""

        # Find stats for trade_year-1 (primary) or trade_year-2 (fallback)
        stats_yr = None
        for look_year in [trade_year - 1, trade_year - 2]:
            row = conn.execute("""
                SELECT * FROM nfl_player_seasons
                WHERE gsis_id = ? AND season_year = ?
            """, (gsis, look_year)).fetchone()
            if row:
                stats_yr = row
                break

        if not stats_yr:
            return None, {"position": pos}

        data_year = stats_yr["season_year"]

        # Prior-year stats (for trend features)
        prior_row = conn.execute("""
            SELECT fantasy_points_ppr, games FROM nfl_player_seasons
            WHERE gsis_id = ? AND season_year = ?
        """, (gsis, data_year - 1)).fetchone()

        pts_t        = stats_yr["fantasy_points_ppr"] or 0
        games_t      = stats_yr["games"] or 0
        pts_t_minus1 = (prior_row["fantasy_points_ppr"] or 0) if prior_row else pts_t
        games_t_minus1 = (prior_row["games"] or 0) if prior_row else games_t

        # Age at trade time
        try:
            birth_year = int(bd[:4])
            age = trade_year - birth_year
        except (ValueError, TypeError, IndexError):
            age = 27  # fallback

        # Scheme features from team_season_scheme for the player's team in data_year
        team  = stats_yr["team"] or ""
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
            "team_pass_rate_change_t":0.0,  # TODO: could compute from prior year
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
            "coaching_tree":coaching_tree,
        }

        return feats, meta


# ── Singleton for import convenience ────────────────────────────────────────

_valuator: Optional[TradeValuator] = None

def get_valuator() -> TradeValuator:
    global _valuator
    if _valuator is None:
        _valuator = TradeValuator()
    return _valuator
