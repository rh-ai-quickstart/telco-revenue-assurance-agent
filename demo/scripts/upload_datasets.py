#!/usr/bin/env python3
"""
Download ML training datasets from Hugging Face and upload them to S3.

Datasets:
  Omer1234567/telco-churn-synthetic → data/synthetic_customer_data_evenly_distributed.csv
  fenar/revenue_assurance           → data/telecom_revass_data.csv.xz  (lzma-compressed)
  fenar/starlink                    → data/starlink_data.csv

S3 credentials are read from environment variables matching the my-data-connection secret:
  AWS_S3_ENDPOINT, AWS_S3_BUCKET, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY

Usage:
  python demo/scripts/upload_datasets.py
"""

import lzma
import logging
import os
import tempfile

import boto3
import botocore.client
from datasets import load_dataset

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

DATASETS = [
    {
        "hf_id":    "Omer1234567/telco-churn-synthetic",
        "s3_key":   "data/synthetic_customer_data_evenly_distributed.csv",
        "fmt":      "csv",
        "label":    "Churn",
    },
    {
        "hf_id":    "fenar/revenue_assurance",
        "s3_key":   "data/telecom_revass_data.csv.xz",
        "fmt":      "csv.xz",
        "label":    "Fraud/RAFM",
    },
    {
        "hf_id":    "fenar/starlink",
        "s3_key":   "data/starlink_data.csv",
        "fmt":      "csv",
        "label":    "Starlink QoE",
    },
]


def make_s3_client():
    endpoint = os.environ["AWS_S3_ENDPOINT"]
    if not endpoint.startswith(("http://", "https://")):
        endpoint = "http://" + endpoint
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        config=botocore.client.Config(signature_version="s3v4"),
        verify=False,
    )


def upload(client, bucket, key, local_path):
    client.upload_file(local_path, bucket, key)
    size = client.head_object(Bucket=bucket, Key=key)["ContentLength"]
    log.info("  Uploaded s3://%s/%s (%d bytes)", bucket, key, size)


def main():
    bucket = os.environ.get("AWS_S3_BUCKET", "mybucket")
    client = make_s3_client()

    with tempfile.TemporaryDirectory() as tmp:
        for ds in DATASETS:
            log.info("── %s (%s) ──", ds["label"], ds["hf_id"])
            log.info("  Downloading from Hugging Face...")
            df = load_dataset(ds["hf_id"], split="train").to_pandas()
            log.info("  %d rows × %d columns", len(df), len(df.columns))

            local_path = os.path.join(tmp, os.path.basename(ds["s3_key"]))

            if ds["fmt"] == "csv.xz":
                with lzma.open(local_path, "wt", encoding="utf-8") as f:
                    df.to_csv(f, index=False)
            else:
                df.to_csv(local_path, index=False)

            upload(client, bucket, ds["s3_key"], local_path)

    log.info("All datasets uploaded to s3://%s/data/", bucket)


if __name__ == "__main__":
    main()
