#!/usr/bin/env python3
"""
Telco Revenue Assurance – Synthetic Data Generator

Generates a joined customer/transaction store from three independent
HuggingFace datasets and optionally seeds a PostgreSQL database.

Datasets (no shared join key – relationships are synthetic):
  Omer1234567/telco-churn-synthetic  ~113k billing-cycle rows  (has CustomerID)
  fenar/revenue_assurance            1M transaction rows        (no CustomerID)
  fenar/starlink                     100k location rows         (no CustomerID)

Four-step workflow:
  1. Extract one row per unique CustomerID from the churn dataset
     (latest billing cycle + avg_bill_amount across all cycles)
  2. Cluster Starlink lat/lon into N geographic regions
  3. Assign each customer to a region (tunable QoE-churn coupling)
  4. Assign each revenue-assurance transaction a synthetic CustomerID

Quick test (no database):
  pip install -r data_generator/requirements.txt
  python -m data_generator.generate --n-customers 500 --n-transactions 5000 --dry-run

Full load into PostgreSQL:
  python -m data_generator.generate --db-url postgresql://user:pass@host:5432/dbname

  # or via env var:
  DATABASE_URL=postgresql://... python -m data_generator.generate
"""

import argparse
import logging
import os
import sys
import time

import numpy as np
import pandas as pd
from datasets import load_dataset
from sklearn.cluster import MiniBatchKMeans
from sklearn.preprocessing import StandardScaler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── HuggingFace dataset identifiers ───────────────────────────────────────────
HF_CHURN    = "Omer1234567/telco-churn-synthetic"
HF_RAFM     = "fenar/revenue_assurance"
HF_STARLINK = "fenar/starlink"

# Churn feature order — must match pipeline/03-telco-churn-kfp-pipeline.py data_prep
CHURN_FEATURES = [
    "CallMinutes", "DataUsageGB", "SMSCount", "InternetSessions",
    "TotalBillAmount", "PaymentStatus", "LateFee", "DiscountsApplied",
    "SupportInteractionCount", "PrimaryIssueType", "ResolutionTimeMinutes",
    "SatisfactionScore", "EscalationFlag", "SupportChannel",
]

# ── DDL (used only when pushing to PostgreSQL) ────────────────────────────────
DDL_CUSTOMERS = """
CREATE TABLE IF NOT EXISTS customers (
    customer_id               VARCHAR(20)  PRIMARY KEY,
    has_churned               BOOLEAN      NOT NULL,
    avg_bill_amount           FLOAT,
    call_minutes              INTEGER,
    data_usage_gb             FLOAT,
    sms_count                 INTEGER,
    internet_sessions         INTEGER,
    total_bill_amount         FLOAT,
    payment_status            VARCHAR(20),
    late_fee                  FLOAT,
    discounts_applied         FLOAT,
    support_interaction_count INTEGER,
    primary_issue_type        VARCHAR(50),
    resolution_time_minutes   INTEGER,
    satisfaction_score        INTEGER,
    escalation_flag           BOOLEAN,
    support_channel           VARCHAR(20),
    region_id                 INTEGER,
    latitude                  FLOAT,
    longitude                 FLOAT,
    elevation_m               FLOAT,
    visible_satellites        INTEGER,
    serving_satellites        INTEGER,
    signal_loss_db            FLOAT,
    season                    VARCHAR(20),
    weather                   VARCHAR(30),
    qoe_score                 FLOAT,
    qoe_download_mbps         FLOAT,
    qoe_upload_mbps           FLOAT,
    qoe_latency_ms            FLOAT,
    predicted_churn           BOOLEAN,
    churn_probability         FLOAT,
    predicted_qoe_score       FLOAT,
    predictions_scored_at     TIMESTAMP
);
"""

DDL_TRANSACTIONS = """
CREATE TABLE IF NOT EXISTS transactions (
    transaction_id             BIGSERIAL    PRIMARY KEY,
    customer_id                VARCHAR(20)  NOT NULL REFERENCES customers(customer_id),
    call_duration              FLOAT,
    data_usage                 FLOAT,
    sms_count                  INTEGER,
    roaming_indicator          SMALLINT,
    mobile_wallet_use          SMALLINT,
    plan_type                  VARCHAR(20),
    cost                       FLOAT,
    cellular_location_distance FLOAT,
    personal_pin_used          SMALLINT,
    avg_call_duration          FLOAT,
    avg_data_usage             FLOAT,
    avg_cost                   FLOAT,
    fraud                      SMALLINT     NOT NULL,
    predicted_fraud            BOOLEAN
);
"""

# ── Column rename maps (HuggingFace → snake_case DB column names) ──────────────
CUST_COL_MAP = {
    "CallMinutes":             "call_minutes",
    "DataUsageGB":             "data_usage_gb",
    "SMSCount":                "sms_count",
    "InternetSessions":        "internet_sessions",
    "TotalBillAmount":         "total_bill_amount",
    "PaymentStatus":           "payment_status",
    "LateFee":                 "late_fee",
    "DiscountsApplied":        "discounts_applied",
    "SupportInteractionCount": "support_interaction_count",
    "PrimaryIssueType":        "primary_issue_type",
    "ResolutionTimeMinutes":   "resolution_time_minutes",
    "SatisfactionScore":       "satisfaction_score",
    "EscalationFlag":          "escalation_flag",
    "SupportChannel":          "support_channel",
}

TX_COL_MAP = {
    "Call_Duration":              "call_duration",
    "Data_Usage":                 "data_usage",
    "Sms_Count":                  "sms_count",
    "Roaming_Indicator":          "roaming_indicator",
    "MobileWallet_Use":           "mobile_wallet_use",
    "Plan_Type":                  "plan_type",
    "Cost":                       "cost",
    "Cellular_Location_Distance": "cellular_location_distance",
    "Personal_Pin_Used":          "personal_pin_used",
    "Avg_Call_Duration":          "avg_call_duration",
    "Avg_Data_Usage":             "avg_data_usage",
    "Avg_Cost":                   "avg_cost",
    "fraud":                      "fraud",
}


# ── Step 1 – Extract customers from churn dataset ─────────────────────────────
def load_customers(
    local_path: str | None,
    n_customers: int,
    snapshot_date: str,
    rng: np.random.Generator,
) -> pd.DataFrame:
    if local_path:
        log.info("[1/4] Loading churn data from local file: %s", local_path)
        raw = pd.read_csv(local_path)
    else:
        log.info("[1/4] Downloading churn dataset from HuggingFace (%s)...", HF_CHURN)
        raw = load_dataset(HF_CHURN, split="train").to_pandas()

    log.info("      %d billing-cycle rows, %d columns", len(raw), len(raw.columns))

    # avg_bill_amount: mean TotalBillAmount across ALL billing cycles per customer
    # computed before any filtering so it reflects full history
    avg_bill = (
        raw.groupby("CustomerID")["TotalBillAmount"]
        .mean()
        .rename("avg_bill_amount")
    )

    # Dataset structure: every customer's history ends with ONE HasChurned=True cycle.
    # Taking the latest cycle always → ~99% churn rate (not useful for demo).
    # Taking the last-False cycle → 77% of customers are ON THE PRECIPICE of churn,
    # so the model predicts high risk for almost everyone (no contrast for demo).
    #
    # Strategy: sample ONE RANDOM billing cycle per customer.
    # Early cycles → low-risk feature profile (good satisfaction, no escalation)
    # Late cycles  → high-risk feature profile (low satisfaction, escalations)
    # Result: ~25% HasChurned=True (ground truth churned), ~75% HasChurned=False
    # (mix of low-risk early and high-risk late), giving the model meaningful
    # variation — some customers clearly safe, others clearly at-risk.
    #
    # Business interpretation in the demo:
    #   has_churned=True  → already churned (lost customer, historical)
    #   has_churned=False → still active; agent scores these to find at-risk ones
    #   predict_churn()=1 → AT RISK → deploy retention offer (gift, discount, etc.)
    #   predict_churn()=0 → SAFE    → no immediate action needed
    _ = snapshot_date  # kept as CLI param for Helm chart compatibility / future use
    raw["_date"] = pd.to_datetime(raw["BillingCycleStart"])

    snap_idx  = (
        raw.groupby("CustomerID")
        .apply(lambda g: g.sample(1, random_state=int(rng.integers(2**31))).index[0],
               include_groups=False)
    )
    customers = raw.loc[snap_idx].drop(columns="_date").join(avg_bill, on="CustomerID")
    customers = customers.reset_index(drop=True)

    log.info("      %d unique customers extracted (random billing cycle per customer)", len(customers))

    if n_customers and n_customers < len(customers):
        customers = (
            customers.sample(n=n_customers, random_state=int(rng.integers(2**31)))
            .reset_index(drop=True)
        )
        log.info("      Sampled to %d customers (--n-customers)", len(customers))

    # Normalise identifiers
    customers = customers.rename(columns={"CustomerID": "customer_id", "HasChurned": "has_churned"})
    customers["has_churned"]       = customers["has_churned"].astype(bool)
    customers["PrimaryIssueType"]  = customers["PrimaryIssueType"].fillna("")
    customers["SupportChannel"]    = customers["SupportChannel"].fillna("")

    log.info(
        "      Churn rate: %.1f%%  |  avg_bill_amount range: %.2f – %.2f",
        customers["has_churned"].mean() * 100,
        customers["avg_bill_amount"].min(),
        customers["avg_bill_amount"].max(),
    )
    return customers


# ── Step 2 – Build QoE-tier regions from Starlink dataset ─────────────────────
def build_regions(local_path: str | None, n_regions: int, rng: np.random.Generator) -> pd.DataFrame:
    """
    Split the Starlink dataset into N equal QoE-score buckets (tiers) instead of
    geographic clusters. This creates regions with clearly distinct satellite
    conditions so KServe returns a wide spread of predicted QoE scores, making
    the churn-QoE correlation visible in the demo (poor-QoE region → high churn).
    """
    if local_path:
        log.info("[2/4] Loading Starlink data from local file: %s", local_path)
        df = pd.read_csv(local_path)
    else:
        log.info("[2/4] Downloading Starlink dataset from HuggingFace (%s)...", HF_STARLINK)
        df = load_dataset(HF_STARLINK, split="train").to_pandas()

    log.info("      %d Starlink rows loaded", len(df))

    # Derive latency_ms from packet_loss: map [0.001, 0.011] → [35, 80] ms (Starlink range)
    pl_min, pl_max = df["packet_loss"].min(), df["packet_loss"].max()
    df["_latency_ms"] = 35 + (df["packet_loss"] - pl_min) / (pl_max - pl_min) * 45

    # Assign each row to a QoE tier (0 = worst, n_regions-1 = best)
    # pd.qcut creates equal-sized buckets based on qoe_score quantiles.
    df["cluster"] = pd.qcut(df["qoe_score"], q=n_regions, labels=False)

    def _mode(s):
        m = s.mode()
        return m.iloc[0] if not m.empty else ""

    regions = (
        df.groupby("cluster")
        .agg(
            latitude          =("latitude",               "mean"),
            longitude         =("longitude",              "mean"),
            elevation_m       =("elevation_m",            "mean"),
            visible_satellites=("visible_satellites",     "mean"),
            serving_satellites=("serving_satellites",     "mean"),
            signal_loss_db    =("signal_loss_db",         "mean"),
            season            =("season",                 _mode),
            weather           =("weather",                _mode),
            qoe_score         =("qoe_score",              "mean"),
            qoe_download_mbps =("download_throughput_bps","mean"),
            qoe_upload_mbps   =("upload_throughput_bps",  "mean"),
            qoe_latency_ms    =("_latency_ms",            "mean"),
        )
        .reset_index()
        .rename(columns={"cluster": "region_id"})
    )
    # Round satellite fields to realistic integer/float precision
    regions["visible_satellites"] = regions["visible_satellites"].round().astype(int)
    regions["serving_satellites"] = regions["serving_satellites"].round().astype(int)
    # Convert bps → Mbps
    regions["qoe_download_mbps"] = regions["qoe_download_mbps"] / 1_000_000
    regions["qoe_upload_mbps"]   = regions["qoe_upload_mbps"]   / 1_000_000

    log.info(
        "      Regions by QoE tier:\n%s",
        regions[["region_id", "season", "qoe_score", "visible_satellites",
                 "serving_satellites", "signal_loss_db",
                 "qoe_download_mbps", "qoe_upload_mbps", "qoe_latency_ms"]]
        .round(1)
        .to_string(index=False),
    )
    # Return region summaries AND the full tiered df so assign_regions()
    # can sample individual rows per customer instead of using region means.
    return regions, df


# ── Step 3 – Assign customers to regions (tunable QoE-churn coupling) ─────────
def assign_regions(
    customers:   pd.DataFrame,
    regions:     pd.DataFrame,
    starlink_df: pd.DataFrame,
    coupling:    float,
    rng:         np.random.Generator,
) -> pd.DataFrame:
    """
    coupling=0.0  random region assignment (no correlation with churn)
    coupling=1.0  churned customers → worst-QoE regions; others → best-QoE regions

    Each customer is assigned one individual starlink row sampled from within
    their region cluster, so KServe receives diverse per-customer feature values
    instead of identical region means.
    """
    n   = len(customers)
    n_r = len(regions)

    churn   = customers["has_churned"].astype(float).values   # 0.0 or 1.0
    noise   = rng.uniform(0.0, 1.0, n)
    badness = coupling * churn + (1.0 - coupling) * noise     # higher → worse QoE

    # rank[i] = position of customer i when badness is sorted ascending (0=least bad)
    rank = np.argsort(np.argsort(badness))

    # Sort regions ascending: index 0 = worst QoE, index n_r-1 = best QoE
    r_sorted = regions.sort_values("qoe_score").reset_index(drop=True)

    # Map rank to region: rank=n-1 (most bad) → region 0 (worst QoE)
    region_pos = np.floor((1.0 - (rank + 0.5) / n) * n_r).astype(int)
    region_pos = np.clip(region_pos, 0, n_r - 1)

    # Actual cluster ID for each customer (r_sorted preserves original region_id)
    assigned_cluster = r_sorted["region_id"].values[region_pos]

    # Build a lookup: cluster_id → list of row indices in starlink_df
    cluster_to_rows = {
        cid: grp.index.tolist()
        for cid, grp in starlink_df.groupby("cluster")
    }

    # Sample one individual starlink row per customer from their assigned cluster.
    # This gives each customer unique satellite conditions so KServe returns
    # a spread of QoE predictions rather than identical region-mean scores.
    sampled_idx = np.array([
        rng.choice(cluster_to_rows[cid])
        for cid in assigned_cluster
    ])
    sampled = starlink_df.loc[sampled_idx].reset_index(drop=True)

    out = customers.copy()
    out["region_id"]          = assigned_cluster
    out["latitude"]           = sampled["latitude"].values
    out["longitude"]          = sampled["longitude"].values
    out["elevation_m"]        = sampled["elevation_m"].values
    out["visible_satellites"] = sampled["visible_satellites"].values.astype(int)
    out["serving_satellites"] = sampled["serving_satellites"].values.astype(int)
    out["signal_loss_db"]     = sampled["signal_loss_db"].values
    out["season"]             = sampled["season"].values
    out["weather"]            = sampled["weather"].values
    out["qoe_score"]          = sampled["qoe_score"].values
    out["qoe_download_mbps"]  = sampled["download_throughput_bps"].values / 1_000_000
    out["qoe_upload_mbps"]    = sampled["upload_throughput_bps"].values  / 1_000_000
    out["qoe_latency_ms"]     = sampled["_latency_ms"].values

    mean_qoe_churned    = out.loc[out["has_churned"],  "qoe_score"].mean()
    mean_qoe_notchurned = out.loc[~out["has_churned"], "qoe_score"].mean()
    log.info(
        "[3/4] Region assignment | coupling=%.2f | "
        "mean QoE churned=%.1f  not-churned=%.1f | "
        "QoE range %.1f–%.1f | download %.0f–%.0f Mbps | latency %.0f–%.0f ms",
        coupling,
        mean_qoe_churned, mean_qoe_notchurned,
        out["qoe_score"].min(), out["qoe_score"].max(),
        out["qoe_download_mbps"].min(), out["qoe_download_mbps"].max(),
        out["qoe_latency_ms"].min(),    out["qoe_latency_ms"].max(),
    )
    return out


# ── Step 4 – Load transactions and assign synthetic CustomerIDs ───────────────
def load_transactions(
    local_path:    str | None,
    n_transactions: int,
    customers:     pd.DataFrame,
    strategy:      str,
    rng:           np.random.Generator,
) -> pd.DataFrame:
    if local_path:
        log.info("[4/4] Loading RAFM data from local file: %s", local_path)
        df = pd.read_csv(local_path)
    else:
        log.info("[4/4] Downloading revenue_assurance dataset from HuggingFace (%s)...", HF_RAFM)
        df = load_dataset(HF_RAFM, split="train").to_pandas()

    log.info("      %d transaction rows loaded", len(df))

    if n_transactions and n_transactions < len(df):
        df = (
            df.sample(n=n_transactions, random_state=int(rng.integers(2**31)))
            .reset_index(drop=True)
        )
        log.info("      Sampled to %d transactions (--n-transactions)", len(df))

    # Synthetic CustomerID assignment — revenue_assurance has no CustomerID column
    cids    = customers["customer_id"].values
    n_cust  = len(cids)
    n_tx    = len(df)

    weights = None
    if strategy == "weighted":
        w       = customers["avg_bill_amount"].values.clip(0).astype(float)
        weights = w / w.sum()

    # Guarantee every customer gets at least one transaction before weighted distribution.
    guaranteed  = rng.permutation(cids)  # one tx per customer, shuffled
    n_remaining = n_tx - n_cust
    if n_remaining > 0:
        remaining = rng.choice(cids, size=n_remaining, p=weights)
        all_ids   = np.concatenate([guaranteed, remaining])
    else:
        all_ids   = guaranteed[:n_tx]

    df["customer_id"] = rng.permutation(all_ids)  # shuffle so guaranteed aren't all first

    # Normalise Fraud label to lowercase for consistency
    df = df.rename(columns={"Fraud": "fraud"})

    log.info(
        "      Assignment: strategy=%s | distinct customers hit: %d / %d | fraud rate: %.2f%%",
        strategy,
        df["customer_id"].nunique(),
        len(customers),
        df["fraud"].mean() * 100,
    )
    return df


# ── Print summary statistics ──────────────────────────────────────────────────
def print_summary(customers: pd.DataFrame, transactions: pd.DataFrame) -> None:
    sep = "─" * 60
    print(sep)
    print("CUSTOMERS TABLE")
    print(sep)
    print(f"  Rows             : {len(customers):,}")
    print(f"  Churn rate       : {customers['has_churned'].mean()*100:.1f}%")
    print(f"  Avg bill amount  : {customers['avg_bill_amount'].mean():.2f}")
    print(f"  Regions used     : {customers['region_id'].nunique()}")
    print(f"  Mean QoE score   : {customers['qoe_score'].mean():.1f} / 100")
    print(f"  Mean download    : {customers['qoe_download_mbps'].mean():.0f} Mbps")
    print(f"  Mean upload      : {customers['qoe_upload_mbps'].mean():.0f} Mbps")
    print(f"  Mean latency     : {customers['qoe_latency_ms'].mean():.0f} ms")
    print(f"  Payment status   : {customers['PaymentStatus'].value_counts().to_dict()}")
    print(f"\n  Sample rows:")
    print(
        customers[["customer_id", "has_churned", "avg_bill_amount",
                   "region_id", "season", "qoe_score"]]
        .head(5)
        .to_string(index=False)
    )

    print(sep)
    print("TRANSACTIONS TABLE")
    print(sep)
    print(f"  Rows             : {len(transactions):,}")
    print(f"  Fraud rate       : {transactions['fraud'].mean()*100:.2f}%")
    print(f"  Distinct customers: {transactions['customer_id'].nunique():,}")
    print(f"  Plan type split  : {transactions['Plan_Type'].value_counts().to_dict()}")
    print(f"\n  Sample rows:")
    print(
        transactions[["customer_id", "Call_Duration", "Data_Usage", "Cost", "Plan_Type", "fraud"]]
        .head(5)
        .to_string(index=False)
    )
    print(sep)


# ── Push to PostgreSQL ────────────────────────────────────────────────────────
def push_to_db(customers: pd.DataFrame, transactions: pd.DataFrame, db_url: str) -> None:
    from sqlalchemy import create_engine, text

    log.info("Connecting to PostgreSQL...")
    # SQLAlchemy 2.x defaults to psycopg (v3) for postgresql:// URLs.
    # Explicitly request psycopg2 which is installed via psycopg2-binary.
    if db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg2://", 1)
    engine = create_engine(db_url, future=True)

    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS transactions"))
        conn.execute(text("DROP TABLE IF EXISTS customers"))
        conn.execute(text(DDL_CUSTOMERS))
        conn.execute(text(DDL_TRANSACTIONS))
        log.info("Schema (re)created: customers + transactions")

    # Prepare customers for DB (snake_case column names)
    keep_cust = (
        ["customer_id", "has_churned", "avg_bill_amount"]
        + list(CUST_COL_MAP.keys())
        + ["region_id", "latitude", "longitude", "elevation_m",
           "visible_satellites", "serving_satellites", "signal_loss_db",
           "season", "weather",
           "qoe_score", "qoe_download_mbps", "qoe_upload_mbps", "qoe_latency_ms"]
    )
    cust_db = customers[keep_cust].rename(columns=CUST_COL_MAP).copy()
    cust_db["escalation_flag"] = cust_db["escalation_flag"].astype(bool)

    log.info("Inserting %d customers...", len(cust_db))
    t0 = time.monotonic()
    with engine.begin() as conn:
        cust_db.to_sql("customers", conn, if_exists="append", index=False,
                       method="multi", chunksize=5000)
    log.info("  customers done in %.1fs", time.monotonic() - t0)

    # Prepare transactions for DB
    keep_tx = list(TX_COL_MAP.keys()) + ["customer_id"]
    tx_db   = transactions[[c for c in keep_tx if c in transactions.columns]].rename(columns=TX_COL_MAP).copy()

    log.info("Inserting %d transactions (may take a few minutes for full dataset)...", len(tx_db))
    t0 = time.monotonic()
    with engine.begin() as conn:
        tx_db.to_sql("transactions", conn, if_exists="append", index=False,
                     method="multi", chunksize=4000)
    log.info("  transactions done in %.1fs", time.monotonic() - t0)

    with engine.connect() as conn:
        n_c = conn.execute(text("SELECT COUNT(*) FROM customers")).scalar()
        n_t = conn.execute(text("SELECT COUNT(*) FROM transactions")).scalar()
    log.info("DB summary: %d customers  |  %d transactions", n_c, n_t)


# ── CLI ───────────────────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Generate synthetic telco data and optionally seed PostgreSQL",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--n-customers",    type=int,   default=0,
                   help="Max unique customers (0 = all ~27k)")
    p.add_argument("--n-transactions", type=int,   default=0,
                   help="Max transactions (0 = all 1M)")
    p.add_argument("--snapshot-date",  type=str,   default="2021-06-01",
                   help="Point-in-time snapshot date (YYYY-MM-DD). "
                        "Each customer gets the billing cycle nearest to this date. "
                        "Default 2021-06-01 (midpoint of 2020-2022 range) gives ~25%% churn rate.")
    p.add_argument("--n-regions",      type=int,   default=10,
                   help="Number of geographic Starlink clusters")
    p.add_argument("--coupling",       type=float, default=0.5,
                   help="QoE-churn coupling: 0.0=random, 1.0=fully correlated")
    p.add_argument("--max-churn-rate", type=float, default=0.15,
                   help="Cap has_churned=True to this fraction after region assignment "
                        "(default 0.15). Ensures active retention targets exist in every region.")
    p.add_argument("--tx-strategy",    choices=["weighted", "uniform"], default="weighted",
                   help="Transaction-to-customer assignment strategy")
    p.add_argument("--seed",           type=int,   default=42,
                   help="Random seed for reproducibility")
    p.add_argument("--db-url",         type=str,   default=None,
                   help="PostgreSQL connection URL (or set DATABASE_URL env var)")
    p.add_argument("--dry-run",        action="store_true",
                   help="Generate and print stats only — do not push to DB")
    p.add_argument("--save-csv",       action="store_true",
                   help="Save customers.csv and transactions.csv to current directory")
    p.add_argument("--local-churn",    type=str,   default=None,
                   help="Use a local CSV file instead of downloading churn dataset")
    p.add_argument("--local-rafm",     type=str,   default=None,
                   help="Use a local CSV file instead of downloading RAFM dataset")
    p.add_argument("--local-starlink", type=str,   default=None,
                   help="Use a local CSV file instead of downloading Starlink dataset")
    return p.parse_args()


def main() -> None:
    args   = parse_args()
    rng    = np.random.default_rng(args.seed)
    db_url = args.db_url or os.getenv("DATABASE_URL")

    if not args.dry_run and not db_url:
        log.error("No database URL provided. Use --db-url or set DATABASE_URL, or pass --dry-run.")
        sys.exit(1)

    t_start = time.monotonic()

    customers              = load_customers(args.local_churn, args.n_customers, args.snapshot_date, rng)
    regions, starlink_df   = build_regions(args.local_starlink, args.n_regions, rng)
    customers              = assign_regions(customers, regions, starlink_df, args.coupling, rng)

    # Cap has_churned=True to max_churn_rate so every region has active retention targets.
    # The source dataset has ~46% churn; uncapped, high-coupling regions fill with
    # already-churned customers leaving no active targets for the agent to act on.
    max_churned = max(1, int(len(customers) * args.max_churn_rate))
    churned_idx = customers.index[customers["has_churned"]].tolist()
    if len(churned_idx) > max_churned:
        flip = rng.choice(churned_idx, size=len(churned_idx) - max_churned, replace=False)
        customers.loc[flip, "has_churned"] = False
        log.info(
            "Capped has_churned: %d → %d (%.0f%% → %.0f%%)",
            len(churned_idx), max_churned,
            len(churned_idx) / len(customers) * 100,
            max_churned / len(customers) * 100,
        )

    transactions = load_transactions(args.local_rafm, args.n_transactions,
                                     customers, args.tx_strategy, rng)

    elapsed = time.monotonic() - t_start
    log.info(
        "Generation done in %.1fs | %d customers | %d transactions",
        elapsed, len(customers), len(transactions),
    )

    print_summary(customers, transactions)

    if args.save_csv:
        customers.to_csv("customers.csv", index=False)
        transactions.to_csv("transactions.csv", index=False)
        log.info("Saved customers.csv (%d rows) and transactions.csv (%d rows)",
                 len(customers), len(transactions))

    if not args.dry_run:
        push_to_db(customers, transactions, db_url)

    log.info("Total time: %.1fs", time.monotonic() - t_start)


if __name__ == "__main__":
    main()
