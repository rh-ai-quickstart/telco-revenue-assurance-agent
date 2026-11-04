"""Provision and verify a read-only S4 identity for OpenShift model serving.

Requires boto3 and access to the existing training S4 deployment. Credentials
stay in process memory and Secret objects are passed to oc through stdin.
"""

# This CLI prints only nonsecret progress and verification results.
# ruff: noqa: T201

from __future__ import annotations

import argparse
import base64
import contextlib
import datetime
import json
import re
import selectors
import shlex
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import ClientError


class StorageSetup:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.oc = [*shlex.split(args.oc), "-n", args.namespace]
        self.identity = args.serving_user
        if self.identity == args.owner:
            raise RuntimeError("Serving identity must be different from the training owner")
        self.connection_names = [
            f"telco-{model}-model-storage" for model in ("churn", "fraud", "qoe")
        ]

    def command(self, arguments: list[str], document: dict[str, Any] | None = None) -> str:
        result = subprocess.run(  # noqa: S603 -- Explicit argv; secrets use stdin.
            [*self.oc, *arguments],
            input=json.dumps(document) if document is not None else None,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(
                f"Cluster command failed ({result.returncode}); captured output withheld"
            )
        return result.stdout

    def admin(self, *arguments: str) -> dict[str, Any]:
        output = self.command(
            ["exec", f"deployment/{self.args.storage_service}", "--", "radosgw-admin", *arguments]
        )
        return json.loads(output) if output.strip() else {}

    def secret(self, name: str) -> dict[str, Any] | None:
        output = self.command(["get", "secret", name, "--ignore-not-found=true", "-o", "json"])
        return json.loads(output) if output.strip() else None

    @staticmethod
    def decode(secret: dict[str, Any]) -> dict[str, str]:
        return {
            key: base64.b64decode(value, validate=True).decode()
            for key, value in secret.get("data", {}).items()
        }

    def owner_invariants(self, owner: dict[str, Any]) -> dict[str, Any]:
        # Compared only in memory; never report the keys themselves.
        return {
            **{key: owner.get(key) for key in ("user_id", "admin", "system", "op_mask", "caps")},
            "keys": [key for key in owner.get("keys", []) if key["user"] == self.args.owner],
        }

    def serving_keys(self, user: dict[str, Any]) -> dict[str, str]:
        if (
            user.get("user_id") != self.identity
            or user.get("admin")
            or user.get("system")
            or user.get("op_mask") != "read"
            or user.get("caps")
            or user.get("subusers")
        ):
            raise RuntimeError("Serving user does not have exactly read-only non-admin access")
        keys = [key for key in user.get("keys", []) if key["user"] == self.identity]
        if len(keys) != 1 or not keys[0].get("access_key") or not keys[0].get("secret_key"):
            raise RuntimeError(
                "Serving user must have exactly one usable S3 key; refusing rotation"
            )
        return {
            "AWS_ACCESS_KEY_ID": keys[0]["access_key"],
            "AWS_SECRET_ACCESS_KEY": keys[0]["secret_key"],
        }

    @contextlib.contextmanager
    def endpoint(self) -> Iterator[str]:
        forward = subprocess.Popen(  # noqa: S603 -- Explicit argv, localhost binding.
            [
                *self.oc,
                "port-forward",
                "--address=127.0.0.1",
                f"service/{self.args.storage_service}",
                f":{self.args.s3_port}",
                "--pod-running-timeout=30s",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            with selectors.DefaultSelector() as selector:
                if forward.stdout is None:
                    raise RuntimeError("Port-forward output unavailable")
                selector.register(forward.stdout, selectors.EVENT_READ)
                deadline = time.monotonic() + 35
                port = None
                while time.monotonic() < deadline:
                    if forward.poll() is not None:
                        raise RuntimeError("Storage port-forward exited")
                    for key, _ in selector.select(timeout=1):
                        match = re.search(
                            r"Forwarding from 127\.0\.0\.1:(\d+)", key.fileobj.readline()
                        )
                        if match:
                            port = int(match.group(1))
                            break
                    if port:
                        break
                if port is None:
                    raise RuntimeError("Storage port-forward timed out")
            yield f"http://127.0.0.1:{port}"
        finally:
            forward.terminate()
            try:
                forward.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                forward.kill()
                forward.communicate(timeout=5)

    def client(self, endpoint: str, credentials: dict[str, str]) -> BaseClient:
        return boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=self.args.region,
            aws_access_key_id=credentials["AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=credentials["AWS_SECRET_ACCESS_KEY"],
            config=Config(
                signature_version="s3v4",
                connect_timeout=10,
                read_timeout=40,
                retries={"total_max_attempts": 1},
                s3={"addressing_style": "path", "payload_signing_enabled": True},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )

    @staticmethod
    def denied(operation: Callable[..., Any], **kwargs: object) -> dict[str, Any]:
        try:
            operation(**kwargs)
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code == "AccessDenied" and status == 403:
                return {"denied": True, "code": code, "http_status": status}
            raise RuntimeError(f"Expected AccessDenied/403, received {code}/{status}") from None
        raise RuntimeError("Serving identity unexpectedly performed a write/delete operation")

    def read_policy(self, owner: BaseClient) -> dict[str, Any] | None:
        try:
            return json.loads(owner.get_bucket_policy(Bucket=self.args.bucket)["Policy"])
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "NoSuchBucketPolicy":
                return None
            raise

    def policy_with_reader(self, original: dict[str, Any] | None) -> dict[str, Any]:
        policy = (
            json.loads(json.dumps(original))
            if original
            else {"Version": "2012-10-17", "Statement": []}
        )
        if policy.get("Version") != "2012-10-17" or not isinstance(policy.get("Statement"), list):
            raise RuntimeError("Existing bucket policy has an unsupported shape; refusing rewrite")
        principal = {"AWS": [f"arn:aws:iam:::user/{self.identity}"]}
        desired = [
            {
                "Sid": "TelcoModelServingList",
                "Effect": "Allow",
                "Principal": principal,
                "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
                "Resource": [f"arn:aws:s3:::{self.args.bucket}"],
            },
            {
                "Sid": "TelcoModelServingRead",
                "Effect": "Allow",
                "Principal": principal,
                "Action": ["s3:GetObject"],
                "Resource": [f"arn:aws:s3:::{self.args.bucket}/models/*"],
            },
        ]
        for statement in desired:
            matches = [item for item in policy["Statement"] if item.get("Sid") == statement["Sid"]]
            if matches and matches != [statement]:
                raise RuntimeError("Existing serving policy statement differs; refusing overwrite")
            if not matches:
                policy["Statement"].append(statement)
        return policy

    def restore_policy(
        self, owner: BaseClient, original: dict[str, Any] | None, expected: dict[str, Any]
    ) -> None:
        if self.read_policy(owner) != expected:
            raise RuntimeError("Bucket policy changed concurrently; automatic rollback refused")
        if original is None:
            owner.delete_bucket_policy(Bucket=self.args.bucket)
        else:
            owner.put_bucket_policy(Bucket=self.args.bucket, Policy=json.dumps(original))

    def prove_permissions(
        self, owner: BaseClient, reader: BaseClient, report: dict[str, Any]
    ) -> None:
        bucket = self.args.bucket
        key = "models/_telco-serving-access-check/" + str(uuid.uuid4())
        payload = b"Disposable telco serving read-only permission check.\n"
        report["canary_key"] = key
        owns_canary = False
        try:
            # UUID collision check before creating the helper's own object.
            existing = owner.list_objects_v2(Bucket=bucket, Prefix=key, MaxKeys=1)
            if existing.get("Contents"):
                raise RuntimeError("Canary collision; refusing overwrite")
            owns_canary = True
            owner.put_object(Bucket=bucket, Key=key, Body=payload)
            read = reader.get_object(Bucket=bucket, Key=key)
            try:
                if read["Body"].read() != payload:
                    raise RuntimeError("Serving read returned different canary bytes")
            finally:
                read["Body"].close()
            report["get_object_allowed"] = True
            listing = reader.list_objects_v2(Bucket=bucket, Prefix=key, MaxKeys=1)
            if not any(item["Key"] == key for item in listing.get("Contents", [])):
                raise RuntimeError("Serving list did not return the canary")
            report["list_objects_allowed"] = True
            report["put_object"] = self.denied(
                reader.put_object, Bucket=bucket, Key=key, Body=b"must be denied"
            )
            report["delete_object"] = self.denied(reader.delete_object, Bucket=bucket, Key=key)
            check = owner.get_object(Bucket=bucket, Key=key)
            try:
                if check["Body"].read() != payload:
                    raise RuntimeError("Denied write/delete modified canary data")
            finally:
                check["Body"].close()
            report["canary_unchanged_after_denials"] = True
        finally:
            # This key is reserved exclusively for this invocation's disposable check.
            if owns_canary:
                owner.delete_object(Bucket=bucket, Key=key)
                remaining = owner.list_objects_v2(Bucket=bucket, Prefix=key, MaxKeys=1)
                report["canary_removed"] = not remaining.get("Contents")
                if not report["canary_removed"]:
                    raise RuntimeError("Disposable canary cleanup did not complete")

    def provision(self, report: dict[str, Any]) -> None:
        existing = {name: self.secret(name) for name in self.connection_names}
        master = self.secret(self.args.owner_secret)
        if master is None:
            raise RuntimeError("Training storage credential Secret is missing")
        owner_credentials = self.decode(master)
        before = self.admin("user", "info", "--uid", self.args.owner)
        initial_invariants = self.owner_invariants(before)
        users = self.admin("user", "list")
        user_present = self.identity in users
        if not user_present and any(existing.values()):
            raise RuntimeError(
                "Serving Secrets exist without the expected identity; refusing takeover"
            )
        created = False
        policy_changed = False
        original_policy = None
        desired_policy = None
        report["secrets"] = []
        report["stage"] = "identity"
        try:
            if not user_present:
                self.admin(
                    "user",
                    "create",
                    "--uid",
                    self.identity,
                    "--display-name",
                    "Telco model serving read-only",
                    "--op-mask",
                    "read",
                    "--max-buckets",
                    "-1",
                    "--key-type",
                    "s3",
                    "--gen-access-key",
                    "--gen-secret",
                )
                created = True
            after = self.admin("user", "info", "--uid", self.args.owner)
            if self.owner_invariants(after) != initial_invariants:
                raise RuntimeError("Owner keys or permissions changed unexpectedly")
            credentials = self.serving_keys(self.admin("user", "info", "--uid", self.identity))
            if credentials["AWS_ACCESS_KEY_ID"] == owner_credentials["AWS_ACCESS_KEY_ID"]:
                raise RuntimeError("Serving credentials must differ from the training owner")
            values = {
                **credentials,
                "AWS_S3_ENDPOINT": f"http://{self.args.storage_service}.{self.args.namespace}.svc.cluster.local:{self.args.s3_port}",
                "AWS_S3_BUCKET": self.args.bucket,
                "AWS_DEFAULT_REGION": self.args.region,
            }
            labels = {
                "opendatahub.io/managed": "true",
                "opendatahub.io/dashboard": "true",
                "app.kubernetes.io/part-of": "telco-revenue-assurance-agent",
            }
            annotations = {
                "opendatahub.io/connection-type": "s3",
                "opendatahub.io/connection-type-protocol": "s3",
            }
            for name, secret in existing.items():
                if secret is None:
                    continue
                if (
                    self.decode(secret) != values
                    or any(
                        secret.get("metadata", {}).get("labels", {}).get(k) != v
                        for k, v in labels.items()
                    )
                    or any(
                        secret.get("metadata", {}).get("annotations", {}).get(k) != v
                        for k, v in annotations.items()
                    )
                ):
                    raise RuntimeError(f"Existing {name} differs; refusing overwrite")
            with (
                self.endpoint() as endpoint,
                contextlib.closing(self.client(endpoint, owner_credentials)) as owner,
                contextlib.closing(self.client(endpoint, credentials)) as reader,
            ):
                report["stage"] = "bucket_policy"
                original_policy = self.read_policy(owner)
                desired_policy = self.policy_with_reader(original_policy)
                if desired_policy != original_policy:
                    owner.put_bucket_policy(
                        Bucket=self.args.bucket, Policy=json.dumps(desired_policy)
                    )
                    policy_changed = True
                    if self.read_policy(owner) != desired_policy:
                        raise RuntimeError(
                            "Serving bucket policy did not retain the required statements"
                        )
                report["stage"] = "permission_proof"
                self.prove_permissions(owner, reader, report)
            report["owner_keys_permissions_unchanged"] = (
                self.owner_invariants(self.admin("user", "info", "--uid", self.args.owner))
                == initial_invariants
            )
            if not report["owner_keys_permissions_unchanged"]:
                raise RuntimeError("Owner invariant verification failed")
            report["stage"] = "connection_secrets"
            for name, current in existing.items():
                if current is None:
                    self.command(
                        ["create", "-f", "-"],
                        {
                            "apiVersion": "v1",
                            "kind": "Secret",
                            "type": "Opaque",
                            "metadata": {
                                "name": name,
                                "namespace": self.args.namespace,
                                "labels": labels,
                                "annotations": annotations,
                            },
                            "stringData": values,
                        },
                    )
                readback = self.secret(name)
                if readback is None or self.decode(readback) != values:
                    raise RuntimeError(f"Secret verification failed: {name}")
                report["secrets"].append(
                    {
                        "name": name,
                        "action": "created" if current is None else "reused",
                        "verified": True,
                    }
                )
            report["identity_action"] = "created" if created else "reused"
            report["policy_action"] = "extended" if policy_changed else "reused"
            report["object_read_scope"] = f"s3://{self.args.bucket}/models/*"
            report["stage"] = "complete"
            report["status"] = "success"
        except Exception:
            # Never leave a newly created unverified identity behind. Existing
            # identities and identities referenced by a created Secret are retained.
            if not report["secrets"] and not any(
                self.secret(name) for name in self.connection_names
            ):
                if policy_changed and desired_policy is not None:
                    with (
                        self.endpoint() as endpoint,
                        contextlib.closing(self.client(endpoint, owner_credentials)) as owner,
                    ):
                        self.restore_policy(owner, original_policy, desired_policy)
                    report["bucket_policy_restored"] = True
                if created:
                    # No purge-data: this user was created only for read access and
                    # cannot create buckets or objects.
                    self.admin("user", "rm", "--uid", self.identity)
                    report["unverified_identity_removed"] = True
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--oc", default="oc")
    parser.add_argument("--storage-service", default="telco-training-storage")
    parser.add_argument("--owner-secret", default="telco-training-s3")
    parser.add_argument("--owner", default="s4admin")
    parser.add_argument("--serving-user", default="telco-model-serving")
    parser.add_argument("--bucket", default="mybucket")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--s3-port", type=int, default=7480)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    report = {
        "namespace": args.namespace,
        "identity": args.serving_user,
        "bucket": args.bucket,
        "status": "running",
        "credential_values_recorded": False,
        "started_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    try:
        StorageSetup(args).provision(report)
    except Exception as error:
        report["status"] = "failed"
        report["error_type"] = type(error).__name__
        # RuntimeError messages are controlled by this helper; third-party exceptions
        # can include request headers/credentials and are deliberately not rendered.
        if type(error) is RuntimeError:
            report["error"] = str(error)
        elif isinstance(error, ClientError):
            report["error_code"] = error.response.get("Error", {}).get("Code")
            report["http_status"] = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    report["finished_at"] = datetime.datetime.now(datetime.UTC).isoformat()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if report["status"] != "success":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
