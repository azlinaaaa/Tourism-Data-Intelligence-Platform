"""End-to-end ETL, analytics and ML pipeline for the tourism data hub.

Run from the project root:
    python src/run_pipeline.py
"""

from __future__ import annotations

import json
import math
import warnings
from dataclasses import dataclass
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    average_precision_score,
    brier_score_loss,
    confusion_matrix,
    f1_score,
    precision_recall_curve,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, OrdinalEncoder, StandardScaler

warnings.filterwarnings("ignore", category=FutureWarning)

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
PROCESSED = ROOT / "data" / "processed"
QUALITY = ROOT / "data" / "quality"
OUTPUTS = ROOT / "outputs"
MODELS = ROOT / "models"

RANDOM_STATE = 42


@dataclass(frozen=True)
class ModelCandidate:
    family: str
    variant: str
    pipeline: Pipeline
    parameter_note: str


def ensure_directories() -> None:
    for folder in (PROCESSED, QUALITY, OUTPUTS, MODELS):
        folder.mkdir(parents=True, exist_ok=True)


def month_number(series: pd.Series) -> pd.Series:
    months = {
        "January": 1,
        "February": 2,
        "March": 3,
        "April": 4,
        "May": 5,
        "June": 6,
        "July": 7,
        "August": 8,
        "September": 9,
        "October": 10,
        "November": 11,
        "December": 12,
    }
    return series.map(months).astype("Int64")


def clean_bookings(raw: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = raw.copy()
    df.columns = [c.strip().lower() for c in df.columns]
    df.insert(0, "booking_id", [f"B{i:07d}" for i in range(1, len(df) + 1)])

    object_cols = df.select_dtypes(include="object").columns
    for col in object_cols:
        df[col] = df[col].replace({"NULL": np.nan, "": np.nan})

    df["arrival_month_number"] = month_number(df["arrival_date_month"])
    df["arrival_date"] = pd.to_datetime(
        {
            "year": df["arrival_date_year"],
            "month": df["arrival_month_number"],
            "day": df["arrival_date_day_of_month"],
        },
        errors="coerce",
    )
    df["date_key"] = df["arrival_date"].dt.strftime("%Y%m%d").astype("Int64")

    df["children_missing_flag"] = df["children"].isna().astype(int)
    df["children"] = df["children"].fillna(0)
    df["country"] = df["country"].fillna("UNK")
    df["agent_present"] = df["agent"].notna().astype(int)
    df["company_present"] = df["company"].notna().astype(int)

    df["total_nights"] = df["stays_in_weekend_nights"] + df["stays_in_week_nights"]
    df["total_guests"] = df["adults"] + df["children"] + df["babies"]
    df["adr_invalid_flag"] = ((df["adr"] < 0) | df["adr"].isna()).astype(int)
    df["adr_clean"] = df["adr"].clip(lower=0)
    df["invalid_guest_flag"] = (df["total_guests"] <= 0).astype(int)
    df["invalid_date_flag"] = df["arrival_date"].isna().astype(int)
    df["valid_for_ml"] = (
        (df["invalid_guest_flag"] == 0)
        & (df["invalid_date_flag"] == 0)
        & df["is_canceled"].isin([0, 1])
    ).astype(int)

    df["booking_status"] = np.where(df["is_canceled"].eq(1), "Cancelled", "Stayed")
    df["gross_booking_value"] = df["adr_clean"] * df["total_nights"]
    df["realized_revenue"] = np.where(
        df["is_canceled"].eq(0), df["gross_booking_value"], 0.0
    )
    df["duplicate_signature_flag"] = df.drop(columns="booking_id").duplicated(
        keep=False
    ).astype(int)

    quality_rows = [
        ("Source rows", len(df), "Information", "All rows retained"),
        (
            "Fully duplicated signatures",
            int(df["duplicate_signature_flag"].sum()),
            "Review",
            "Retained: no natural booking key exists, so identical rows may be separate bookings",
        ),
        ("Missing country", int((df["country"] == "UNK").sum()), "Imputed", "Filled as UNK"),
        (
            "Missing children",
            int(df["children_missing_flag"].sum()),
            "Imputed",
            "Filled with 0 and flagged",
        ),
        (
            "Invalid negative/missing ADR",
            int(df["adr_invalid_flag"].sum()),
            "Corrected",
            "Clipped to 0 and flagged",
        ),
        (
            "Zero-guest bookings",
            int(df["invalid_guest_flag"].sum()),
            "Excluded from ML",
            "Retained in fact table with quality flag",
        ),
        (
            "Invalid arrival dates",
            int(df["invalid_date_flag"].sum()),
            "Excluded from ML",
            "Retained in fact table with quality flag",
        ),
    ]
    quality = pd.DataFrame(
        quality_rows, columns=["quality_check", "row_count", "action", "rationale"]
    )
    return df, quality


def clean_arrivals(raw: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = raw.copy()
    df.columns = [c.strip().lower() for c in df.columns]
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    numeric_cols = ["arrivals", "arrivals_male", "arrivals_female"]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).clip(lower=0).astype(int)
    df["country"] = df["country"].fillna("XXX")
    df["soe"] = df["soe"].fillna("Unknown")
    df["date_key"] = df["date"].dt.strftime("%Y%m%d").astype("Int64")
    df["gender_reconciliation_gap"] = (
        df["arrivals"] - df["arrivals_male"] - df["arrivals_female"]
    )
    quality = pd.DataFrame(
        [
            (
                "Arrival source rows",
                len(df),
                "Information",
                "All rows retained",
            ),
            (
                "Missing/invalid arrival dates",
                int(df["date"].isna().sum()),
                "Flagged",
                "Do not use for time analysis",
            ),
            (
                "Gender reconciliation exceptions",
                int(df["gender_reconciliation_gap"].ne(0).sum()),
                "Review",
                "arrivals should equal male + female",
            ),
        ],
        columns=["quality_check", "row_count", "action", "rationale"],
    )
    return df, quality


def build_dimensions(bookings: pd.DataFrame, arrivals: pd.DataFrame) -> None:
    min_date = min(bookings["arrival_date"].min(), arrivals["date"].min())
    max_date = max(bookings["arrival_date"].max(), arrivals["date"].max())
    dates = pd.DataFrame({"date": pd.date_range(min_date, max_date, freq="D")})
    dates["date_key"] = dates["date"].dt.strftime("%Y%m%d").astype(int)
    dates["year"] = dates["date"].dt.year
    dates["quarter"] = "Q" + dates["date"].dt.quarter.astype(str)
    dates["month_number"] = dates["date"].dt.month
    dates["month_name"] = dates["date"].dt.month_name()
    dates["year_month"] = dates["date"].dt.strftime("%Y-%m")
    dates["week_number"] = dates["date"].dt.isocalendar().week.astype(int)
    dates["day_name"] = dates["date"].dt.day_name()
    dates["is_weekend"] = dates["date"].dt.dayofweek.ge(5).astype(int)
    dates.to_csv(PROCESSED / "dim_date.csv", index=False)

    channels = sorted(bookings["distribution_channel"].fillna("Unknown").unique())
    pd.DataFrame(
        {"channel_key": range(1, len(channels) + 1), "distribution_channel": channels}
    ).to_csv(PROCESSED / "dim_channel.csv", index=False)

    segments = sorted(bookings["market_segment"].fillna("Unknown").unique())
    pd.DataFrame(
        {"segment_key": range(1, len(segments) + 1), "market_segment": segments}
    ).to_csv(PROCESSED / "dim_segment.csv", index=False)

    hotels = sorted(bookings["hotel"].fillna("Unknown").unique())
    pd.DataFrame({"hotel_key": range(1, len(hotels) + 1), "hotel": hotels}).to_csv(
        PROCESSED / "dim_hotel.csv", index=False
    )


def write_fact_tables(bookings: pd.DataFrame, arrivals: pd.DataFrame) -> pd.DataFrame:
    channel_map = {
        v: i + 1
        for i, v in enumerate(sorted(bookings["distribution_channel"].fillna("Unknown").unique()))
    }
    segment_map = {
        v: i + 1
        for i, v in enumerate(sorted(bookings["market_segment"].fillna("Unknown").unique()))
    }
    hotel_map = {
        v: i + 1 for i, v in enumerate(sorted(bookings["hotel"].fillna("Unknown").unique()))
    }

    fact = bookings.copy()
    fact["channel_key"] = fact["distribution_channel"].fillna("Unknown").map(channel_map)
    fact["segment_key"] = fact["market_segment"].fillna("Unknown").map(segment_map)
    fact["hotel_key"] = fact["hotel"].fillna("Unknown").map(hotel_map)
    fact_columns = [
        "booking_id",
        "date_key",
        "arrival_date",
        "channel_key",
        "segment_key",
        "hotel_key",
        "country",
        "booking_status",
        "is_canceled",
        "lead_time",
        "total_nights",
        "total_guests",
        "adults",
        "children",
        "babies",
        "meal",
        "customer_type",
        "deposit_type",
        "reserved_room_type",
        "is_repeated_guest",
        "previous_cancellations",
        "previous_bookings_not_canceled",
        "agent_present",
        "company_present",
        "adr_clean",
        "gross_booking_value",
        "realized_revenue",
        "required_car_parking_spaces",
        "total_of_special_requests",
        "valid_for_ml",
        "invalid_guest_flag",
        "adr_invalid_flag",
        "children_missing_flag",
    ]
    fact = fact[fact_columns]
    fact.to_csv(PROCESSED / "fact_bookings.csv", index=False)

    arrival_fact = arrivals[
        [
            "date_key",
            "date",
            "country",
            "soe",
            "arrivals",
            "arrivals_male",
            "arrivals_female",
            "gender_reconciliation_gap",
        ]
    ].copy()
    arrival_fact.to_csv(PROCESSED / "fact_market_arrivals.csv", index=False)
    return fact


def generate_campaign_funnel(bookings: pd.DataFrame) -> pd.DataFrame:
    """Create a deterministic, explicitly synthetic API/campaign funnel table."""
    rng = np.random.default_rng(RANDOM_STATE)
    months = pd.date_range("2016-01-01", "2017-08-01", freq="MS")
    channel_config = {
        # delivery, open/view, click, lead, booking, target ROAS
        "WhatsApp API": (0.975, 0.68, 0.16, 0.22, 0.16, 15.0),
        "Email": (0.965, 0.34, 0.11, 0.25, 0.13, 18.0),
        "Google Search": (0.995, 0.82, 0.18, 0.30, 0.18, 5.5),
        "Facebook/Instagram": (0.990, 0.56, 0.09, 0.24, 0.13, 4.2),
        "Affiliate": (0.995, 0.74, 0.14, 0.28, 0.20, 7.0),
    }
    month_demand = (
        bookings.assign(year_month=bookings["arrival_date"].dt.to_period("M").astype(str))
        .groupby("year_month")
        .size()
        .to_dict()
    )
    rows: list[dict[str, object]] = []
    campaign_id = 1
    for month in months:
        ym = month.strftime("%Y-%m")
        demand_factor = month_demand.get(ym, 4000) / 4000
        season = 1 + 0.12 * math.sin(month.month / 12 * 2 * math.pi)
        for channel, (delivery, open_rate, ctr, lead_rate, conversion, target_roas) in channel_config.items():
            audience = int(rng.integers(16000, 42000) * demand_factor * season)
            sent = max(audience, 100)
            delivered = int(sent * np.clip(delivery + rng.normal(0, 0.008), 0.85, 1))
            opened = int(delivered * np.clip(open_rate + rng.normal(0, 0.025), 0.05, 0.95))
            clicks = int(opened * np.clip(ctr + rng.normal(0, 0.012), 0.01, 0.45))
            leads = int(clicks * np.clip(lead_rate + rng.normal(0, 0.02), 0.03, 0.60))
            booked = int(leads * np.clip(conversion + rng.normal(0, 0.018), 0.02, 0.55))
            avg_order_value = rng.uniform(2200, 5200)
            attributed_revenue = round(booked * avg_order_value, 2)
            achieved_roas = max(target_roas + rng.normal(0, target_roas * 0.12), 2.0)
            spend = round(max(attributed_revenue / achieved_roas, 500), 2)
            rows.append(
                {
                    "campaign_id": f"C{campaign_id:04d}",
                    "date_key": int(month.strftime("%Y%m%d")),
                    "campaign_month": month.date().isoformat(),
                    "campaign_name": f"{channel} {month.strftime('%b %Y')} Demand Push",
                    "channel": channel,
                    "target_segment": rng.choice(
                        ["Families", "Young Professionals", "Repeat Customers", "General Market"]
                    ),
                    "sent": sent,
                    "delivered": delivered,
                    "opened": opened,
                    "clicks": clicks,
                    "leads": leads,
                    "bookings": booked,
                    "spend_myr": spend,
                    "attributed_revenue_myr": attributed_revenue,
                    "data_type": "SYNTHETIC - portfolio demonstration only",
                }
            )
            campaign_id += 1
    result = pd.DataFrame(rows)
    result.to_csv(PROCESSED / "fact_campaign_funnel_synthetic.csv", index=False)
    return result


def model_frame(bookings: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series, pd.DataFrame]:
    valid = bookings.loc[bookings["valid_for_ml"].eq(1)].sort_values(
        ["arrival_date", "booking_id"]
    )
    features = [
        "hotel",
        "lead_time",
        "arrival_month_number",
        "arrival_date_week_number",
        "arrival_date_day_of_month",
        "stays_in_weekend_nights",
        "stays_in_week_nights",
        "total_nights",
        "adults",
        "children",
        "babies",
        "total_guests",
        "meal",
        "country",
        "market_segment",
        "distribution_channel",
        "is_repeated_guest",
        "previous_cancellations",
        "previous_bookings_not_canceled",
        "reserved_room_type",
        "deposit_type",
        "agent_present",
        "company_present",
        "customer_type",
        "adr_clean",
        "required_car_parking_spaces",
        "total_of_special_requests",
    ]
    return valid[features], valid["is_canceled"].astype(int), valid


def candidates(numeric: list[str], categorical: list[str]) -> list[ModelCandidate]:
    linear_pre = ColumnTransformer(
        [
            (
                "numeric",
                Pipeline(
                    [
                        ("imputer", SimpleImputer(strategy="median")),
                        ("scaler", StandardScaler()),
                    ]
                ),
                numeric,
            ),
            (
                "categorical",
                Pipeline(
                    [
                        ("imputer", SimpleImputer(strategy="most_frequent")),
                        (
                            "encoder",
                            OneHotEncoder(handle_unknown="ignore", min_frequency=20),
                        ),
                    ]
                ),
                categorical,
            ),
        ]
    )
    tree_pre = ColumnTransformer(
        [
            ("numeric", SimpleImputer(strategy="median"), numeric),
            (
                "categorical",
                Pipeline(
                    [
                        ("imputer", SimpleImputer(strategy="most_frequent")),
                        (
                            "encoder",
                            OrdinalEncoder(
                                handle_unknown="use_encoded_value", unknown_value=-1
                            ),
                        ),
                    ]
                ),
                categorical,
            ),
        ]
    )
    return [
        ModelCandidate(
            "Logistic Regression",
            "Logistic Regression | C=0.2",
            Pipeline(
                [
                    ("preprocess", linear_pre),
                    (
                        "model",
                        LogisticRegression(
                            C=0.2,
                            max_iter=700,
                            class_weight="balanced",
                            random_state=RANDOM_STATE,
                        ),
                    ),
                ]
            ),
            "C=0.2; class_weight=balanced",
        ),
        ModelCandidate(
            "Logistic Regression",
            "Logistic Regression | C=1.0",
            Pipeline(
                [
                    ("preprocess", linear_pre),
                    (
                        "model",
                        LogisticRegression(
                            C=1.0,
                            max_iter=700,
                            class_weight="balanced",
                            random_state=RANDOM_STATE,
                        ),
                    ),
                ]
            ),
            "C=1.0; class_weight=balanced",
        ),
        ModelCandidate(
            "Random Forest",
            "Random Forest | depth=12 leaf=5",
            Pipeline(
                [
                    ("preprocess", tree_pre),
                    (
                        "model",
                        RandomForestClassifier(
                            n_estimators=140,
                            max_depth=12,
                            min_samples_leaf=5,
                            max_features="sqrt",
                            class_weight="balanced_subsample",
                            n_jobs=-1,
                            random_state=RANDOM_STATE,
                        ),
                    ),
                ]
            ),
            "140 trees; max_depth=12; min_samples_leaf=5",
        ),
        ModelCandidate(
            "Random Forest",
            "Random Forest | depth=18 leaf=3",
            Pipeline(
                [
                    ("preprocess", tree_pre),
                    (
                        "model",
                        RandomForestClassifier(
                            n_estimators=180,
                            max_depth=18,
                            min_samples_leaf=3,
                            max_features="sqrt",
                            class_weight="balanced_subsample",
                            n_jobs=-1,
                            random_state=RANDOM_STATE,
                        ),
                    ),
                ]
            ),
            "180 trees; max_depth=18; min_samples_leaf=3",
        ),
        ModelCandidate(
            "Histogram Gradient Boosting",
            "HGB | leaves=15 lr=0.10",
            Pipeline(
                [
                    ("preprocess", tree_pre),
                    (
                        "model",
                        HistGradientBoostingClassifier(
                            learning_rate=0.10,
                            max_iter=160,
                            max_leaf_nodes=15,
                            min_samples_leaf=30,
                            l2_regularization=1.0,
                            random_state=RANDOM_STATE,
                        ),
                    ),
                ]
            ),
            "learning_rate=0.10; max_leaf_nodes=15; l2=1.0",
        ),
        ModelCandidate(
            "Histogram Gradient Boosting",
            "HGB | leaves=31 lr=0.08",
            Pipeline(
                [
                    ("preprocess", tree_pre),
                    (
                        "model",
                        HistGradientBoostingClassifier(
                            learning_rate=0.08,
                            max_iter=180,
                            max_leaf_nodes=31,
                            min_samples_leaf=25,
                            l2_regularization=1.0,
                            random_state=RANDOM_STATE,
                        ),
                    ),
                ]
            ),
            "learning_rate=0.08; max_leaf_nodes=31; l2=1.0",
        ),
        ModelCandidate(
            "Histogram Gradient Boosting",
            "HGB | leaves=63 lr=0.05",
            Pipeline(
                [
                    ("preprocess", tree_pre),
                    (
                        "model",
                        HistGradientBoostingClassifier(
                            learning_rate=0.05,
                            max_iter=220,
                            max_leaf_nodes=63,
                            min_samples_leaf=20,
                            l2_regularization=2.0,
                            random_state=RANDOM_STATE,
                        ),
                    ),
                ]
            ),
            "learning_rate=0.05; max_leaf_nodes=63; l2=2.0",
        ),
    ]


def best_f1_threshold(y_true: pd.Series, probability: np.ndarray) -> float:
    precision, recall, thresholds = precision_recall_curve(y_true, probability)
    f1 = 2 * precision[:-1] * recall[:-1] / np.maximum(
        precision[:-1] + recall[:-1], 1e-12
    )
    return float(thresholds[int(np.nanargmax(f1))])


def score_model(
    family: str,
    variant: str,
    y_true: pd.Series,
    probability: np.ndarray,
    threshold: float,
    split: str,
    parameter_note: str,
) -> dict[str, object]:
    pred = (probability >= threshold).astype(int)
    return {
        "model": family,
        "variant": variant,
        "split": split,
        "roc_auc": roc_auc_score(y_true, probability),
        "pr_auc": average_precision_score(y_true, probability),
        "accuracy": accuracy_score(y_true, pred),
        "precision": precision_score(y_true, pred, zero_division=0),
        "recall": recall_score(y_true, pred, zero_division=0),
        "f1": f1_score(y_true, pred, zero_division=0),
        "brier_score": brier_score_loss(y_true, probability),
        "threshold": threshold,
        "parameters": parameter_note,
    }


def train_models(bookings: pd.DataFrame) -> dict[str, object]:
    X, y, valid = model_frame(bookings)
    numeric = X.select_dtypes(exclude="object").columns.tolist()
    categorical = X.select_dtypes(include="object").columns.tolist()

    n = len(X)
    train_end = int(n * 0.70)
    valid_end = int(n * 0.85)
    X_train, y_train = X.iloc[:train_end], y.iloc[:train_end]
    X_valid, y_valid = X.iloc[train_end:valid_end], y.iloc[train_end:valid_end]
    X_test, y_test = X.iloc[valid_end:], y.iloc[valid_end:]

    validation_rows: list[dict[str, object]] = []
    fitted: dict[str, Pipeline] = {}
    thresholds: dict[str, float] = {}
    candidate_notes: dict[str, str] = {}
    candidate_families: dict[str, str] = {}

    for candidate in candidates(numeric, categorical):
        candidate.pipeline.fit(X_train, y_train)
        p_valid = candidate.pipeline.predict_proba(X_valid)[:, 1]
        threshold = best_f1_threshold(y_valid, p_valid)
        validation_rows.append(
            score_model(
                candidate.family,
                candidate.variant,
                y_valid,
                p_valid,
                threshold,
                "Validation",
                candidate.parameter_note,
            )
        )
        fitted[candidate.variant] = candidate.pipeline
        thresholds[candidate.variant] = threshold
        candidate_notes[candidate.variant] = candidate.parameter_note
        candidate_families[candidate.variant] = candidate.family

    validation_df = pd.DataFrame(validation_rows).sort_values(
        ["roc_auc", "pr_auc"], ascending=False
    )
    validation_df.to_csv(OUTPUTS / "hyperparameter_tuning_results.csv", index=False)
    selected_validation = (
        validation_df.sort_values(["roc_auc", "pr_auc"], ascending=False)
        .groupby("model", as_index=False, sort=False)
        .head(1)
    )
    best_family = str(selected_validation.iloc[0]["model"])
    best_variant = str(selected_validation.iloc[0]["variant"])

    test_rows: list[dict[str, object]] = []
    X_train_valid = X.iloc[:valid_end]
    y_train_valid = y.iloc[:valid_end]
    for row in selected_validation.itertuples(index=False):
        variant = str(row.variant)
        family = str(row.model)
        pipeline = fitted[variant]
        pipeline.fit(X_train_valid, y_train_valid)
        p_test = pipeline.predict_proba(X_test)[:, 1]
        test_rows.append(
            score_model(
                family,
                variant,
                y_test,
                p_test,
                thresholds[variant],
                "Holdout Test",
                candidate_notes[variant],
            )
        )

    comparison = pd.concat([validation_df, pd.DataFrame(test_rows)], ignore_index=True)
    comparison.to_csv(OUTPUTS / "model_comparison.csv", index=False)

    best_pipeline = fitted[best_variant]
    best_probability = best_pipeline.predict_proba(X_test)[:, 1]
    best_threshold = thresholds[best_variant]
    best_pred = (best_probability >= best_threshold).astype(int)
    cm = confusion_matrix(y_test, best_pred)
    pd.DataFrame(
        cm,
        index=["Actual Stayed", "Actual Cancelled"],
        columns=["Predicted Stayed", "Predicted Cancelled"],
    ).to_csv(OUTPUTS / "confusion_matrix.csv")

    test_meta = valid.iloc[valid_end:].copy()
    predictions = pd.DataFrame(
        {
            "booking_id": test_meta["booking_id"].values,
            "arrival_date": test_meta["arrival_date"].dt.strftime("%Y-%m-%d").values,
            "hotel": test_meta["hotel"].values,
            "country": test_meta["country"].values,
            "market_segment": test_meta["market_segment"].values,
            "distribution_channel": test_meta["distribution_channel"].values,
            "gross_booking_value": test_meta["gross_booking_value"].round(2).values,
            "actual_is_canceled": y_test.values,
            "cancellation_probability": best_probability,
            "predicted_is_canceled": best_pred,
        }
    )
    predictions["risk_band"] = pd.cut(
        predictions["cancellation_probability"],
        bins=[-np.inf, 0.30, 0.60, np.inf],
        labels=["Low", "Medium", "High"],
    ).astype(str)
    predictions.to_csv(OUTPUTS / "cancellation_predictions_holdout.csv", index=False)

    # Model-agnostic feature importance: shuffle one original feature at a time.
    rng = np.random.default_rng(RANDOM_STATE)
    sample_n = min(7000, len(X_test))
    sample_index = rng.choice(len(X_test), sample_n, replace=False)
    X_imp = X_test.iloc[sample_index].copy()
    y_imp = y_test.iloc[sample_index]
    baseline_auc = roc_auc_score(y_imp, best_pipeline.predict_proba(X_imp)[:, 1])
    importance_rows = []
    for col in X_imp.columns:
        shuffled = X_imp.copy()
        shuffled[col] = rng.permutation(shuffled[col].to_numpy())
        shuffled_auc = roc_auc_score(y_imp, best_pipeline.predict_proba(shuffled)[:, 1])
        importance_rows.append(
            {"feature": col, "importance_auc_drop": baseline_auc - shuffled_auc}
        )
    pd.DataFrame(importance_rows).sort_values(
        "importance_auc_drop", ascending=False
    ).to_csv(OUTPUTS / "feature_importance.csv", index=False)

    joblib.dump(
        {
            "pipeline": best_pipeline,
            "threshold": best_threshold,
            "features": X.columns.tolist(),
            "model_name": best_family,
            "model_variant": best_variant,
        },
        MODELS / "cancellation_risk_model.joblib",
    )

    best_test = pd.DataFrame(test_rows).query("variant == @best_variant").iloc[0].to_dict()
    summary = {
        "best_model": best_family,
        "best_variant": best_variant,
        "decision_threshold": best_threshold,
        "train_rows": len(X_train_valid),
        "test_rows": len(X_test),
        "train_date_min": str(valid.iloc[0]["arrival_date"].date()),
        "train_date_max": str(valid.iloc[valid_end - 1]["arrival_date"].date()),
        "test_date_min": str(valid.iloc[valid_end]["arrival_date"].date()),
        "test_date_max": str(valid.iloc[-1]["arrival_date"].date()),
        "holdout_metrics": {
            key: float(best_test[key])
            for key in [
                "roc_auc",
                "pr_auc",
                "accuracy",
                "precision",
                "recall",
                "f1",
                "brier_score",
            ]
        },
        "leakage_exclusions": [
            "reservation_status",
            "reservation_status_date",
            "assigned_room_type",
            "booking_changes",
            "days_in_waiting_list",
        ],
    }
    (OUTPUTS / "model_summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def write_analysis_tables(
    bookings: pd.DataFrame, campaigns: pd.DataFrame, quality: pd.DataFrame
) -> None:
    monthly = (
        bookings.assign(year_month=bookings["arrival_date"].dt.to_period("M").astype(str))
        .groupby("year_month", as_index=False)
        .agg(
            bookings=("booking_id", "count"),
            cancelled=("is_canceled", "sum"),
            gross_booking_value=("gross_booking_value", "sum"),
            realized_revenue=("realized_revenue", "sum"),
            adr_sum=("adr_clean", "sum"),
            adr_count=("adr_clean", "count"),
            guests=("total_guests", "sum"),
        )
    )
    monthly.to_csv(OUTPUTS / "monthly_kpis.csv", index=False)

    channel = (
        bookings.groupby("distribution_channel", dropna=False, as_index=False)
        .agg(
            bookings=("booking_id", "count"),
            cancelled=("is_canceled", "sum"),
            gross_booking_value=("gross_booking_value", "sum"),
            realized_revenue=("realized_revenue", "sum"),
        )
        .sort_values("bookings", ascending=False)
    )
    channel.to_csv(OUTPUTS / "channel_kpis.csv", index=False)

    customer = (
        bookings.groupby("customer_type", dropna=False, as_index=False)
        .agg(
            bookings=("booking_id", "count"),
            cancelled=("is_canceled", "sum"),
            gross_booking_value=("gross_booking_value", "sum"),
            realized_revenue=("realized_revenue", "sum"),
            average_lead_time=("lead_time", "mean"),
        )
        .sort_values("bookings", ascending=False)
    )
    customer.to_csv(OUTPUTS / "customer_kpis.csv", index=False)

    campaign_summary = (
        campaigns.groupby("channel", as_index=False)
        .agg(
            sent=("sent", "sum"),
            delivered=("delivered", "sum"),
            opened=("opened", "sum"),
            clicks=("clicks", "sum"),
            leads=("leads", "sum"),
            bookings=("bookings", "sum"),
            spend_myr=("spend_myr", "sum"),
            attributed_revenue_myr=("attributed_revenue_myr", "sum"),
        )
        .sort_values("attributed_revenue_myr", ascending=False)
    )
    campaign_summary.to_csv(OUTPUTS / "campaign_channel_kpis.csv", index=False)
    quality.to_csv(QUALITY / "data_quality_report.csv", index=False)

    sample_cols = [
        "booking_id",
        "arrival_date",
        "hotel",
        "country",
        "distribution_channel",
        "market_segment",
        "customer_type",
        "lead_time",
        "total_nights",
        "total_guests",
        "adr_clean",
        "gross_booking_value",
        "realized_revenue",
        "booking_status",
        "valid_for_ml",
    ]
    bookings[sample_cols].head(2000).to_csv(OUTPUTS / "booking_sample_2000.csv", index=False)


def main() -> None:
    ensure_directories()
    raw_bookings = pd.read_csv(RAW / "hotel_bookings.csv", low_memory=False)
    raw_arrivals = pd.read_csv(RAW / "arrivals_soe.csv", low_memory=False)

    bookings, booking_quality = clean_bookings(raw_bookings)
    arrivals, arrival_quality = clean_arrivals(raw_arrivals)
    quality = pd.concat([booking_quality, arrival_quality], ignore_index=True)

    build_dimensions(bookings, arrivals)
    write_fact_tables(bookings, arrivals)
    campaigns = generate_campaign_funnel(bookings)
    model_summary = train_models(bookings)
    write_analysis_tables(bookings, campaigns, quality)

    run_summary = {
        "raw_booking_rows": len(raw_bookings),
        "processed_booking_rows": len(bookings),
        "raw_arrival_rows": len(raw_arrivals),
        "processed_arrival_rows": len(arrivals),
        "synthetic_campaign_rows": len(campaigns),
        "best_model": model_summary["best_model"],
    }
    (OUTPUTS / "pipeline_run_summary.json").write_text(json.dumps(run_summary, indent=2))
    print(json.dumps(run_summary, indent=2))


if __name__ == "__main__":
    main()
