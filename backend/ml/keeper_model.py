"""
Logistic Regression model: P(player is kept by same manager next season).

Training data: our league's historical roster + keeper decisions (240 keeper
decisions across 4 Sleeper seasons).

Validation: leave-one-season-out cross-validation (train on 3, test on 1,
rotate).  Reports accuracy, AUC-ROC, and calibration.

Features:
  - position (encoded)
  - age at decision point
  - prior season PPR
  - games played (durability signal)
  - times already kept by this manager (loyalty/investment)
  - target share / carries (opportunity security)
  - position scarcity proxy (how many similar players on the roster)

Usage:
    python keeper_model.py     # train + LOSO-CV + save
"""

import sys
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import roc_auc_score, log_loss
from sklearn.pipeline import Pipeline
from sklearn.calibration import calibration_curve

sys.path.insert(0, str(Path(__file__).parent.parent))
from db.database import get_connection

MODEL_PATH = Path(__file__).parent / "keeper_model.pkl"
POS_MAP = {"QB": 0, "RB": 1, "WR": 2, "TE": 3}


def build_keeper_dataset(conn) -> pd.DataFrame:
    """
    For each player on a roster at end of season Y, create a row with:
    - features describing the player at end of season Y
    - label: 1 if they were kept in season Y+1, 0 otherwise

    Injury features (from player_season_injuries):
      weeks_out     — weeks listed as report_status='Out' during the season
      injury_bucket — 0=none  1=soft-tissue  2=upper  3=lower-joint  4=head/neck
    Combined with ppr_per_game these let the model distinguish "injured but
    still productive per game" from "healthy but just bad."
    """
    df = pd.read_sql_query("""
        WITH roster_end AS (
            -- Final roster snapshot for each team each season
            SELECT DISTINCT
                rp.player_id,
                s.year,
                t.manager_id,
                m.display_name  AS manager,
                p.full_name,
                p.position,
                p.birth_date,
                p.gsis_id,
                CAST(s.year - SUBSTR(p.birth_date,1,4) AS INTEGER) AS age
            FROM roster_players rp
            JOIN seasons s   ON s.id  = rp.season_id
            JOIN teams t     ON t.id  = rp.team_id
            JOIN managers m  ON m.id  = t.manager_id
            JOIN players p   ON p.id  = rp.player_id
            WHERE s.platform = 'sleeper'
              AND rp.week = 0
              AND p.position IN ('QB','RB','WR','TE')
              AND p.birth_date IS NOT NULL
        ),
        keeper_decisions AS (
            SELECT
                re.player_id,
                re.year,
                re.manager_id,
                re.manager,
                re.full_name,
                re.position,
                re.age,
                re.gsis_id,
                -- Was this player kept the following year BY THE SAME MANAGER?
                CASE WHEN dp_next.id IS NOT NULL THEN 1 ELSE 0 END AS was_kept
            FROM roster_end re
            -- next season exists?
            JOIN seasons ns ON ns.year = re.year + 1 AND ns.platform = 'sleeper'
            -- same manager's team next year
            JOIN teams nt ON nt.season_id = ns.id AND nt.manager_id = re.manager_id
            -- was there a keeper pick for this player?
            LEFT JOIN draft_picks dp_next
                ON dp_next.season_id = ns.id
               AND dp_next.team_id   = nt.id
               AND dp_next.player_id = re.player_id
               AND dp_next.is_keeper = 1
        )
        SELECT
            kd.*,
            -- Prior season raw stats
            psa.fantasy_points_ppr  AS prior_ppr,
            psa.games               AS prior_games,
            psa.target_share        AS prior_target_share,
            psa.carries             AS prior_carries,
            psa.wopr                AS prior_wopr,
            -- Times kept league-wide before this decision (any manager, any team).
            (SELECT COUNT(*) FROM draft_picks dk2
             JOIN seasons sk2 ON dk2.season_id = sk2.id
             WHERE dk2.player_id  = kd.player_id
               AND dk2.is_keeper  = 1
               AND sk2.year       < kd.year) AS times_kept_before,
            -- Injury context for this season
            COALESCE(psi.weeks_out,     0) AS weeks_out,
            COALESCE(psi.injury_bucket, 0) AS injury_bucket
        FROM keeper_decisions kd
        LEFT JOIN player_season_advanced psa
            ON psa.player_id  = kd.player_id
           AND psa.season_year = kd.year
        LEFT JOIN player_season_injuries psi
            ON psi.gsis_id     = kd.gsis_id
           AND psi.season_year  = kd.year
        WHERE psa.fantasy_points_ppr IS NOT NULL   -- need production data
    """, conn)

    return df


FEATURE_COLS = [
    "pos_enc", "age",
    "ppr_per_game",           # rate-based production (filters out injury volume loss)
    "prior_games",            # volume / availability signal
    "prior_target_share", "prior_carries", "prior_wopr",
    "times_kept_before",
    "weeks_out",              # injury severity: 0 = healthy, 7+ = likely on IR
    "injury_bucket",          # 0=none 1=soft-tissue 2=upper 3=lower-joint 4=head/neck
]


def prepare_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["pos_enc"] = df["position"].map(POS_MAP).fillna(2).astype(int)
    df["prior_target_share"] = df["prior_target_share"].fillna(0)
    df["prior_carries"]      = df["prior_carries"].fillna(0)
    df["prior_wopr"]         = df["prior_wopr"].fillna(0)
    df["prior_games"]        = df["prior_games"].fillna(0).clip(lower=1)
    df["weeks_out"]          = df["weeks_out"].fillna(0)
    df["injury_bucket"]      = df["injury_bucket"].fillna(0)
    # Rate-based production: PPR per game played (de-noises injury-shortened seasons)
    df["ppr_per_game"]       = df["prior_ppr"] / df["prior_games"]
    return df


def loso_cv(df: pd.DataFrame) -> dict:
    """Leave-one-season-out cross-validation."""
    seasons = sorted(df["year"].unique())
    all_y_true, all_y_prob = [], []
    season_results = {}

    for held_out in seasons:
        train = df[df["year"] != held_out]
        test  = df[df["year"] == held_out]

        if len(train) < 20 or len(test) < 5:
            continue

        X_train = train[FEATURE_COLS].values
        y_train = train["was_kept"].values
        X_test  = test[FEATURE_COLS].values
        y_test  = test["was_kept"].values

        pipe = Pipeline([
            ("scaler", StandardScaler()),
            ("lr", LogisticRegression(C=1.0, max_iter=500, random_state=42)),
        ])
        pipe.fit(X_train, y_train)
        probs = pipe.predict_proba(X_test)[:, 1]

        all_y_true.extend(y_test.tolist())
        all_y_prob.extend(probs.tolist())

        auc = roc_auc_score(y_test, probs) if len(np.unique(y_test)) > 1 else float("nan")
        acc = (probs >= 0.5).astype(int)
        acc = (acc == y_test).mean()

        season_results[held_out] = {
            "n": len(y_test),
            "keep_rate": round(y_test.mean(), 3),
            "auc": round(auc, 3),
            "accuracy": round(acc, 3),
        }

    all_y_true = np.array(all_y_true)
    all_y_prob = np.array(all_y_prob)

    overall_auc = roc_auc_score(all_y_true, all_y_prob)
    overall_acc = ((all_y_prob >= 0.5) == all_y_true).mean()
    ll = log_loss(all_y_true, all_y_prob)

    # Calibration: do predicted probabilities match observed rates?
    frac_pos, mean_pred = calibration_curve(all_y_true, all_y_prob, n_bins=5)
    calibration = list(zip(
        [round(p, 3) for p in mean_pred],
        [round(f, 3) for f in frac_pos]
    ))

    return {
        "by_season": season_results,
        "overall_auc": round(overall_auc, 3),
        "overall_accuracy": round(overall_acc, 3),
        "log_loss": round(ll, 3),
        "calibration": calibration,
        "n_total": len(all_y_true),
        "base_keep_rate": round(all_y_true.mean(), 3),
    }


def train_final_model(df: pd.DataFrame) -> Pipeline:
    """Train on all available data for production use."""
    X = df[FEATURE_COLS].values
    y = df["was_kept"].values
    pipe = Pipeline([
        ("scaler", StandardScaler()),
        ("lr", LogisticRegression(C=1.0, max_iter=500, random_state=42)),
    ])
    pipe.fit(X, y)
    return pipe


def print_report(df: pd.DataFrame, cv_results: dict):
    print("\n" + "=" * 60)
    print("KEEPER PROBABILITY MODEL VALIDATION REPORT")
    print("Leave-one-season-out cross-validation (Sleeper 2022–2025)")
    print("=" * 60)

    print(f"\n  Dataset: {cv_results['n_total']} keeper decisions")
    print(f"  Base keep rate: {cv_results['base_keep_rate']:.1%} "
          f"(random-guess AUC = 0.500)")

    print(f"\n  Overall AUC:      {cv_results['overall_auc']:.3f}")
    print(f"  Overall Accuracy: {cv_results['overall_accuracy']:.1%}")
    print(f"  Log Loss:         {cv_results['log_loss']:.3f}")

    print("\n  By held-out season:")
    print(f"  {'Season':<10} {'N':>5} {'Keep%':>8} {'AUC':>7} {'Accuracy':>10}")
    print("  " + "-" * 45)
    for season, res in sorted(cv_results["by_season"].items()):
        print(f"  {season:<10} {res['n']:>5} {res['keep_rate']:>8.1%} "
              f"{res['auc']:>7.3f} {res['accuracy']:>10.1%}")

    print("\n  Calibration (predicted prob → actual keep rate):")
    print(f"  {'Predicted':>12} {'Actual':>10}")
    for pred_p, act_p in cv_results["calibration"]:
        bar_pred = "█" * int(pred_p * 20)
        bar_act  = "█" * int(act_p * 20)
        print(f"  {pred_p:>12.1%} → {act_p:>8.1%}  pred:{bar_pred:<20} act:{bar_act}")

    # Feature analysis
    print("\n  Keep rate by position × age bracket:")
    df["age_bucket"] = pd.cut(df["age"],
                              bins=[0, 23, 26, 29, 45],
                              labels=["≤23", "24-26", "27-29", "30+"])
    summary = (df.groupby(["position", "age_bucket"])["was_kept"]
               .agg(["mean", "count"])
               .round(3))
    print(summary.to_string())

    # PPR/game threshold analysis
    print("\n  Keep rate by prior PPR/game tier:")
    df["ppr_pg_tier"] = pd.cut(df["ppr_per_game"],
                               bins=[0, 7, 12, 17, 25, 9999],
                               labels=["<7", "7-12", "12-17", "17-25", "25+"])
    summary2 = (df.groupby("ppr_pg_tier")["was_kept"]
                .agg(["mean", "count"])
                .round(3))
    print(summary2.to_string())

    # Injury bucket breakdown
    print("\n  Keep rate by injury bucket:")
    bucket_labels = {0: "0-none", 1: "1-soft-tissue", 2: "2-upper", 3: "3-lower-joint", 4: "4-head/neck"}
    df["bucket_label"] = df["injury_bucket"].map(bucket_labels)
    summary3 = (df.groupby("bucket_label")["was_kept"]
                .agg(["mean", "count"])
                .round(3))
    print(summary3.to_string())

    # Weeks out breakdown
    print("\n  Keep rate by weeks out:")
    df["weeks_out_tier"] = pd.cut(df["weeks_out"],
                                  bins=[-1, 0, 2, 5, 8, 99],
                                  labels=["0 (healthy)", "1-2", "3-5", "6-8", "9+"])
    summary4 = (df.groupby("weeks_out_tier")["was_kept"]
                .agg(["mean", "count"])
                .round(3))
    print(summary4.to_string())


def load_model() -> dict:
    with open(MODEL_PATH, "rb") as f:
        return pickle.load(f)


def predict_keeper_prob(player_features: dict, model_bundle = None) -> float:
    """Return P(player is kept next season) given current-season features."""
    if model_bundle is None:
        model_bundle = load_model()
    pipe = model_bundle["model"]
    feats = model_bundle["features"]
    X = np.array([[player_features.get(f, 0) for f in feats]])
    return float(pipe.predict_proba(X)[0, 1])


if __name__ == "__main__":
    conn = get_connection()
    print("Building keeper dataset…")
    df = build_keeper_dataset(conn)
    conn.close()

    print(f"  {len(df)} decisions  |  keep rate: {df['was_kept'].mean():.1%}")
    df = prepare_features(df)

    print("\nRunning LOSO cross-validation…")
    cv_results = loso_cv(df)
    print_report(df, cv_results)

    print("\nTraining final model on all data…")
    pipe = train_final_model(df)
    with open(MODEL_PATH, "wb") as f:
        pickle.dump({"model": pipe, "features": FEATURE_COLS}, f)
    print(f"Model saved → {MODEL_PATH}")
