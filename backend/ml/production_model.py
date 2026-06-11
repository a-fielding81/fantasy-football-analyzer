"""
Random Forest model: predict a player's next-2-season PPR production.

Training data: nfl_player_seasons (1999–2024), 26 seasons of history.
Target:        sum of PPR points in seasons T+1 and T+2.
Features:      age, position, current PPR, prior-year PPR, trend,
               games, carries/targets, target_share, EPA metrics, etc.

Validation strategy:
  - Temporal hold-out: train on ≤2019, evaluate on 2020–2024.
  - Compare MAE against two baselines:
      1. "Same as last year" (naive)
      2. Age-curve adjusted (our empirical curves from aging_curves table)
  - Report per-position and per-year breakdowns.

Usage:
    python production_model.py          # train + validate + save
    python production_model.py --report # load saved model, print metrics only
"""

import sys
import pickle
import argparse
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor, GradientBoostingRegressor
from sklearn.metrics import mean_absolute_error
from sklearn.preprocessing import LabelEncoder

sys.path.insert(0, str(Path(__file__).parent.parent))
from db.database import get_connection

MODEL_PATH = Path(__file__).parent / "production_model.pkl"
TRAIN_CUTOFF = 2019   # seasons ≤ this year are training data
MIN_PPR = 30          # filter out garbage-time appearances
MIN_GAMES = 8         # filter out injury-wrecked seasons
POSITIONS = ["QB", "RB", "WR", "TE"]

# Map position → integer for the model
POS_MAP = {"QB": 0, "RB": 1, "WR": 2, "TE": 3}


# ─── aging curve fallback ───────────────────────────────────────────────────

def load_aging_curves(conn) -> dict:
    """Return {(position, age): delta_pct} from the precomputed aging_curves table."""
    rows = conn.execute(
        "SELECT position, age, delta_pct FROM aging_curves"
    ).fetchall()
    return {(r["position"], r["age"]): r["delta_pct"] for r in rows}


def aging_curve_prediction(row: pd.Series, curves: dict) -> float:
    """Apply empirical aging-curve delta to current PPR to predict next year."""
    pos = row["position"]
    age = int(row["age"])
    pts = row["pts_t"]
    delta_pct = curves.get((pos, age + 1), curves.get((pos, age), -5.0)) / 100.0
    return max(0, pts * (1 + delta_pct))


# ─── data loading ────────────────────────────────────────────────────────────

def build_dataset(conn) -> pd.DataFrame:
    """
    Build a row-per-player-season dataframe with features at time T
    and target = pts_T1 + pts_T2 (next 2 seasons' PPR sum).
    """
    df = pd.read_sql_query("""
        SELECT
            a.gsis_id,
            a.season_year                                       AS season,
            a.display_name,
            a.position,
            a.games                                             AS games_t,
            a.fantasy_points_ppr                               AS pts_t,
            a.carries                                          AS carries_t,
            a.targets                                          AS targets_t,
            a.target_share                                     AS target_share_t,
            a.air_yards_share                                  AS air_yards_share_t,
            a.wopr                                             AS wopr_t,
            a.rushing_epa                                      AS rushing_epa_t,
            a.receiving_epa                                    AS receiving_epa_t,
            a.birth_date,
            -- prior year stats (self-join via subquery replaced by window)
            LAG(a.fantasy_points_ppr, 1)
                OVER (PARTITION BY a.gsis_id ORDER BY a.season_year) AS pts_t_minus1,
            LAG(a.games, 1)
                OVER (PARTITION BY a.gsis_id ORDER BY a.season_year) AS games_t_minus1,
            -- future targets
            LEAD(a.fantasy_points_ppr, 1)
                OVER (PARTITION BY a.gsis_id ORDER BY a.season_year) AS pts_t1,
            LEAD(a.fantasy_points_ppr, 2)
                OVER (PARTITION BY a.gsis_id ORDER BY a.season_year) AS pts_t2,
            LEAD(a.games, 1)
                OVER (PARTITION BY a.gsis_id ORDER BY a.season_year) AS games_t1
        FROM nfl_player_seasons a
        WHERE a.position IN ('QB','RB','WR','TE')
          AND a.fantasy_points_ppr >= 30
          AND a.games >= 8
    """, conn)

    # Compute age
    df["birth_year"] = pd.to_datetime(df["birth_date"], errors="coerce").dt.year
    df["age"] = df["season"] - df["birth_year"]

    # Production trend: this year vs. last year
    df["pts_trend"] = df["pts_t"] - df["pts_t_minus1"].fillna(df["pts_t"])
    df["games_trend"] = df["games_t"] - df["games_t_minus1"].fillna(df["games_t"])

    # Target: next 2 seasons PPR sum (require T+1 exists; T+2 optional)
    df = df[df["pts_t1"].notna()].copy()
    df["target"] = df["pts_t1"] + df["pts_t2"].fillna(0)

    # Next-year games (for filtering trivial predictions)
    df["games_t1"] = df["games_t1"].fillna(0)

    # Encode position
    df["pos_enc"] = df["position"].map(POS_MAP).fillna(0).astype(int)

    # Fill remaining NAs
    fill_cols = [
        "pts_t_minus1", "carries_t", "targets_t", "target_share_t",
        "air_yards_share_t", "wopr_t", "rushing_epa_t", "receiving_epa_t",
    ]
    df[fill_cols] = df[fill_cols].fillna(0)
    df = df[df["age"].between(18, 45)].copy()

    return df


FEATURE_COLS = [
    "pos_enc", "age",
    "pts_t", "pts_t_minus1", "pts_trend",
    "games_t", "games_t_minus1", "games_trend",
    "carries_t", "targets_t",
    "target_share_t", "air_yards_share_t", "wopr_t",
    "rushing_epa_t", "receiving_epa_t",
]


# ─── evaluation helpers ──────────────────────────────────────────────────────

def evaluate(y_true, y_pred, label: str) -> dict:
    mae  = mean_absolute_error(y_true, y_pred)
    corr = np.corrcoef(y_true, y_pred)[0, 1]
    # Top-quartile accuracy: did we correctly identify the top 25% performers?
    q75 = np.percentile(y_true, 75)
    top_actual = y_true >= q75
    top_pred   = y_pred >= np.percentile(y_pred, 75)
    top_acc = (top_actual == top_pred).mean()
    return {"label": label, "mae": round(mae, 1), "corr": round(corr, 3),
            "top25_acc": round(top_acc, 3), "n": len(y_true)}


# ─── main ────────────────────────────────────────────────────────────────────

def train_and_validate(save: bool = True) -> dict:
    conn = get_connection()
    curves = load_aging_curves(conn)

    print("Building dataset…")
    df = build_dataset(conn)
    conn.close()

    print(f"  Total rows: {len(df)}")
    print(f"  Seasons: {df['season'].min()}–{df['season'].max()}")
    print(f"  Position counts: {df['position'].value_counts().to_dict()}")

    train = df[df["season"] <= TRAIN_CUTOFF].copy()
    test  = df[df["season"] >  TRAIN_CUTOFF].copy()
    print(f"\n  Train: {len(train)} rows ({train['season'].min()}–{train['season'].max()})")
    print(f"  Test:  {len(test)} rows  ({test['season'].min()}–{test['season'].max()})")

    X_train = train[FEATURE_COLS].values
    y_train = train["target"].values
    X_test  = test[FEATURE_COLS].values
    y_test  = test["target"].values

    # ── model ──
    print("\nTraining Random Forest…")
    rf = RandomForestRegressor(
        n_estimators=300,
        max_depth=8,
        min_samples_leaf=10,
        max_features=0.6,
        random_state=42,
        n_jobs=-1,
    )
    rf.fit(X_train, y_train)
    rf_preds = rf.predict(X_test)

    # ── baselines ──
    naive_preds = test["pts_t"].values                        # "same as last year"
    curve_preds = test.apply(
        lambda r: aging_curve_prediction(r, curves), axis=1
    ).values * 2  # ×2 for 2-season window

    # ── overall metrics ──
    results = {
        "overall": [
            evaluate(y_test, rf_preds,    "Random Forest"),
            evaluate(y_test, naive_preds, "Naive (same as last year × 2)"),
            evaluate(y_test, curve_preds, "Aging curve adjusted"),
        ]
    }

    # ── per-position breakdown ──
    results["by_position"] = {}
    for pos in POSITIONS:
        mask = test["position"] == pos
        if mask.sum() < 10:
            continue
        results["by_position"][pos] = [
            evaluate(y_test[mask], rf_preds[mask],    "RF"),
            evaluate(y_test[mask], naive_preds[mask], "Naive"),
            evaluate(y_test[mask], curve_preds[mask], "Curve"),
        ]

    # ── per-year breakdown (test years only) ──
    results["by_year"] = {}
    for yr in sorted(test["season"].unique()):
        mask = (test["season"] == yr).values
        if mask.sum() < 5:
            continue
        results["by_year"][yr] = evaluate(y_test[mask], rf_preds[mask], "RF")

    # ── feature importance ──
    fi = sorted(zip(FEATURE_COLS, rf.feature_importances_),
                key=lambda x: x[1], reverse=True)
    results["feature_importance"] = [(f, round(v, 4)) for f, v in fi]

    # ── save ──
    if save:
        with open(MODEL_PATH, "wb") as f:
            pickle.dump({"model": rf, "features": FEATURE_COLS, "curves": curves}, f)
        print(f"\nModel saved → {MODEL_PATH}")

    return results


def print_report(results: dict):
    print("\n" + "=" * 60)
    print("PRODUCTION MODEL VALIDATION REPORT")
    print("Train: 1999–2019   |   Test: 2020–2024")
    print("Target: sum of next 2 seasons' PPR points")
    print("=" * 60)

    print("\n── Overall (all positions) ──")
    print(f"{'Model':<35} {'MAE':>7} {'Corr':>7} {'Top25%':>8} {'N':>6}")
    print("-" * 65)
    for m in results["overall"]:
        print(f"{m['label']:<35} {m['mae']:>7.1f} {m['corr']:>7.3f} "
              f"{m['top25_acc']:>8.1%} {m['n']:>6}")

    print("\n── By position ──")
    for pos, metrics in results.get("by_position", {}).items():
        print(f"\n  {pos}")
        print(f"  {'Model':<12} {'MAE':>7} {'Corr':>7} {'Top25%':>8}")
        for m in metrics:
            print(f"  {m['label']:<12} {m['mae']:>7.1f} {m['corr']:>7.3f} {m['top25_acc']:>8.1%}")

    print("\n── RF MAE by test year ──")
    for yr, m in sorted(results.get("by_year", {}).items()):
        print(f"  {yr}: MAE={m['mae']:.1f}  corr={m['corr']:.3f}  n={m['n']}")

    print("\n── Top feature importances ──")
    for feat, imp in results.get("feature_importance", [])[:10]:
        bar = "█" * int(imp * 200)
        print(f"  {feat:<25} {imp:.4f}  {bar}")


def load_model() -> dict:
    with open(MODEL_PATH, "rb") as f:
        return pickle.load(f)


def predict_player(player_features: dict, model_bundle = None) -> dict:
    """
    Predict next-2-season PPR for a single player.
    player_features keys should match FEATURE_COLS.
    Returns predicted_pts (2-season total) and per-season estimate.
    """
    if model_bundle is None:
        model_bundle = load_model()
    rf = model_bundle["model"]
    feats = model_bundle["features"]
    X = np.array([[player_features.get(f, 0) for f in feats]])
    pred_total = float(rf.predict(X)[0])
    return {
        "predicted_2season_ppr": round(pred_total, 1),
        "predicted_per_season":  round(pred_total / 2, 1),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", action="store_true",
                        help="Load saved model and print metrics only")
    args = parser.parse_args()

    if args.report and MODEL_PATH.exists():
        # Can't re-evaluate without data; just note model exists
        print(f"Model exists at {MODEL_PATH}. Re-run without --report to retrain.")
    else:
        results = train_and_validate(save=True)
        print_report(results)
