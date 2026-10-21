from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


INPUT_CSV = Path("./tribal_eye_all.csv")

OUTPUT_DAY = Path("./emails_per_day.csv")
OUTPUT_HOUR = Path("./emails_per_hour.csv")
OUTPUT_MINUTE = Path("./emails_per_minute.csv")
OUTPUT_STATS = Path("./email_statistics.json")


def main() -> None:

    print("Loading CSV...")

    df = pd.read_csv(
        INPUT_CSV,
        low_memory=False,
    )

    if "timestamp" not in df.columns:
        raise ValueError(
            "timestamp column not present."
        )

    print(f"Loaded {len(df):,} emails")

    # ============================================================
    # Parse timestamps
    # ============================================================

    df["timestamp"] = pd.to_datetime(
        df["timestamp"],
        utc=True,
        errors="coerce",
    )

    df = df.dropna(
        subset=["timestamp"]
    )

    print(
        f"Valid timestamp rows: {len(df):,}"
    )

    # ============================================================
    # Emails Per Day
    # ============================================================

    df["day"] = df["timestamp"].dt.date

    emails_per_day = (
        df.groupby("day")
        .size()
        .reset_index(name="email_count")
        .sort_values("day")
    )

    # ============================================================
    # Emails Per Hour
    # ============================================================

    df["hour"] = (
        df["timestamp"]
        .dt.floor("h")
    )

    emails_per_hour = (
        df.groupby("hour")
        .size()
        .reset_index(name="email_count")
        .sort_values("hour")
    )

    # ============================================================
    # Emails Per Minute
    # ============================================================

    df["minute"] = (
        df["timestamp"]
        .dt.floor("min")
    )

    emails_per_minute = (
        df.groupby("minute")
        .size()
        .reset_index(name="email_count")
        .sort_values("minute")
    )

    # ============================================================
    # Continuous Minute Timeline
    # ============================================================

    per_min = (
        emails_per_minute
        .set_index("minute")
    )

    full_minute_index = pd.date_range(
        start=per_min.index.min(),
        end=per_min.index.max(),
        freq="min",
    )

    per_min = (
        per_min.reindex(
            full_minute_index,
            fill_value=0,
        )
    )

    per_min.index.name = "minute"

    # ============================================================
    # Rolling Window Analysis
    # ============================================================

    rolling_5m = (
        per_min["email_count"]
        .rolling(window=5)
        .sum()
    )

    rolling_15m = (
        per_min["email_count"]
        .rolling(window=15)
        .sum()
    )

    rolling_60m = (
        per_min["email_count"]
        .rolling(window=60)
        .sum()
    )

    rolling_5m_peak = int(
        rolling_5m.max()
    )

    rolling_15m_peak = int(
        rolling_15m.max()
    )

    rolling_60m_peak = int(
        rolling_60m.max()
    )

    # ============================================================
    # Find Peak Windows
    # ============================================================

    peak_5m_time = rolling_5m.idxmax()
    peak_15m_time = rolling_15m.idxmax()
    peak_60m_time = rolling_60m.idxmax()

    # ============================================================
    # Statistics
    # ============================================================

    daily_counts = emails_per_day["email_count"]

    minute_counts = emails_per_minute["email_count"]

    peak_day = emails_per_day.loc[
        emails_per_day["email_count"].idxmax()
    ]

    peak_hour = emails_per_hour.loc[
        emails_per_hour["email_count"].idxmax()
    ]

    peak_minute = emails_per_minute.loc[
        emails_per_minute["email_count"].idxmax()
    ]

    stats = {
        "total_emails": int(len(df)),
        "unique_days": int(
            emails_per_day.shape[0]
        ),
        "date_range_start": str(
            df["timestamp"].min()
        ),
        "date_range_end": str(
            df["timestamp"].max()
        ),

        # =====================================================
        # Daily Stats
        # =====================================================

        "avg_emails_per_day": float(
            daily_counts.mean()
        ),

        "median_emails_per_day": float(
            daily_counts.median()
        ),

        "min_emails_per_day": int(
            daily_counts.min()
        ),

        "max_emails_per_day": int(
            daily_counts.max()
        ),

        "p95_emails_per_day": float(
            daily_counts.quantile(0.95)
        ),

        "p99_emails_per_day": float(
            daily_counts.quantile(0.99)
        ),

        # =====================================================
        # Hour Stats
        # =====================================================

        "avg_emails_per_hour": float(
            emails_per_hour["email_count"].mean()
        ),

        "median_emails_per_hour": float(
            emails_per_hour["email_count"].median()
        ),

        "max_emails_per_hour": int(
            emails_per_hour["email_count"].max()
        ),

        "p95_emails_per_hour": float(
            emails_per_hour["email_count"].quantile(
                0.95
            )
        ),

        "p99_emails_per_hour": float(
            emails_per_hour["email_count"].quantile(
                0.99
            )
        ),

        # =====================================================
        # Minute Stats
        # =====================================================

        "avg_emails_per_minute": float(
            minute_counts.mean()
        ),

        "median_emails_per_minute": float(
            minute_counts.median()
        ),

        "min_emails_per_minute": int(
            minute_counts.min()
        ),

        "max_emails_per_minute": int(
            minute_counts.max()
        ),

        "p50_emails_per_minute": float(
            minute_counts.quantile(0.50)
        ),

        "p95_emails_per_minute": float(
            minute_counts.quantile(0.95)
        ),

        "p99_emails_per_minute": float(
            minute_counts.quantile(0.99)
        ),

        # =====================================================
        # Rolling Window Stats
        # =====================================================

        "peak_emails_5m": rolling_5m_peak,
        "peak_emails_15m": rolling_15m_peak,
        "peak_emails_60m": rolling_60m_peak,

        "peak_5m_window_end": str(
            peak_5m_time
        ),

        "peak_15m_window_end": str(
            peak_15m_time
        ),

        "peak_60m_window_end": str(
            peak_60m_time
        ),

        "avg_emails_5m_window": float(
            rolling_5m.mean()
        ),

        "avg_emails_15m_window": float(
            rolling_15m.mean()
        ),

        "avg_emails_60m_window": float(
            rolling_60m.mean()
        ),

        "p95_emails_5m_window": float(
            rolling_5m.quantile(0.95)
        ),

        "p99_emails_5m_window": float(
            rolling_5m.quantile(0.99)
        ),

        "p95_emails_15m_window": float(
            rolling_15m.quantile(0.95)
        ),

        "p99_emails_15m_window": float(
            rolling_15m.quantile(0.99)
        ),

        "p95_emails_60m_window": float(
            rolling_60m.quantile(0.95)
        ),

        "p99_emails_60m_window": float(
            rolling_60m.quantile(0.99)
        ),

        # =====================================================
        # Peak timestamps
        # =====================================================

        "peak_day": str(
            peak_day["day"]
        ),

        "peak_day_count": int(
            peak_day["email_count"]
        ),

        "peak_hour": str(
            peak_hour["hour"]
        ),

        "peak_hour_count": int(
            peak_hour["email_count"]
        ),

        "peak_minute": str(
            peak_minute["minute"]
        ),

        "peak_minute_count": int(
            peak_minute["email_count"]
        ),
    }

    # ============================================================
    # Save Outputs
    # ============================================================

    emails_per_day.to_csv(
        OUTPUT_DAY,
        index=False,
    )

    emails_per_hour.to_csv(
        OUTPUT_HOUR,
        index=False,
    )

    emails_per_minute.to_csv(
        OUTPUT_MINUTE,
        index=False,
    )

    with open(
        OUTPUT_STATS,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            stats,
            f,
            indent=2,
            ensure_ascii=False,
        )

    # ============================================================
    # Console Summary
    # ============================================================

    print("\n==============================")
    print("EMAIL TRAFFIC STATISTICS")
    print("==============================")

    for k, v in stats.items():
        print(f"{k}: {v}")

    print("\n==============================")
    print("PEAK WINDOWS")
    print("==============================")

    print(
        f"Peak emails in any 5-minute window : "
        f"{rolling_5m_peak}"
    )

    print(
        f"Peak emails in any 15-minute window: "
        f"{rolling_15m_peak}"
    )

    print(
        f"Peak emails in any 60-minute window: "
        f"{rolling_60m_peak}"
    )

    print(
        f"Peak 5-minute window ended at : "
        f"{peak_5m_time}"
    )

    print(
        f"Peak 15-minute window ended at: "
        f"{peak_15m_time}"
    )

    print(
        f"Peak 60-minute window ended at: "
        f"{peak_60m_time}"
    )

    print("\n==============================")
    print("TOP 20 BUSIEST DAYS")
    print("==============================")

    print(
        emails_per_day
        .sort_values(
            "email_count",
            ascending=False,
        )
        .head(20)
        .to_string(index=False)
    )

    print("\n==============================")
    print("TOP 20 BUSIEST HOURS")
    print("==============================")

    print(
        emails_per_hour
        .sort_values(
            "email_count",
            ascending=False,
        )
        .head(20)
        .to_string(index=False)
    )

    print("\n==============================")
    print("TOP 20 BUSIEST MINUTES")
    print("==============================")

    print(
        emails_per_minute
        .sort_values(
            "email_count",
            ascending=False,
        )
        .head(20)
        .to_string(index=False)
    )

    print("\n==============================")
    print("FILES WRITTEN")
    print("==============================")

    print(OUTPUT_DAY)
    print(OUTPUT_HOUR)
    print(OUTPUT_MINUTE)
    print(OUTPUT_STATS)


if __name__ == "__main__":
    main()