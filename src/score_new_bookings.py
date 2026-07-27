"""Batch-score new bookings with the saved cancellation-risk model.

The input should follow the public hotel_bookings.csv schema. The target and
outcome fields may be omitted because they are not model features.

Example:
    python src/score_new_bookings.py --input new_bookings.csv --output scored.csv
"""

from __future__ import annotations

import argparse
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from run_pipeline import clean_bookings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--model",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "models"
        / "cancellation_risk_model.joblib",
    )
    args = parser.parse_args()

    raw = pd.read_csv(args.input, low_memory=False)
    if "booking_id" in raw.columns:
        raw = raw.rename(columns={"booking_id": "source_booking_id"})
    if "is_canceled" not in raw.columns:
        raw["is_canceled"] = 0  # placeholder only; never used as a feature
    if "reservation_status" not in raw.columns:
        raw["reservation_status"] = "Unknown"
    if "reservation_status_date" not in raw.columns:
        raw["reservation_status_date"] = pd.NaT

    clean, _ = clean_bookings(raw)
    bundle = joblib.load(args.model)
    features = bundle["features"]
    missing = [column for column in features if column not in clean.columns]
    if missing:
        raise ValueError(f"Missing required source fields after ETL: {missing}")

    probability = bundle["pipeline"].predict_proba(clean[features])[:, 1]
    threshold = float(bundle["threshold"])
    output = pd.DataFrame(
        {
            "booking_id": clean["booking_id"],
            "cancellation_probability": probability,
            "predicted_is_canceled": (probability >= threshold).astype(int),
            "risk_band": pd.cut(
                probability,
                bins=[-np.inf, 0.30, 0.60, np.inf],
                labels=["Low", "Medium", "High"],
            ).astype(str),
            "model_name": bundle["model_name"],
            "model_variant": bundle.get("model_variant", ""),
            "decision_threshold": threshold,
        }
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    output.to_csv(args.output, index=False)
    print(f"Scored {len(output):,} bookings -> {args.output}")


if __name__ == "__main__":
    main()
