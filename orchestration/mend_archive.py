"""Archive: copy what Mend publishes and what people decided to an S3 bucket (created by infra/, Terraform).

    marts/<table>/as_of=<day>/<table>.parquet   the mart tables, as of the latest day they hold
    audit/repairs/<id>/proposal.json            what the agent proposed and the gate's result
    audit/repairs/<id>/decision.json            who approved or rejected it, when, and the comment
    audit/events.jsonl                          every build, proposal and decision, in order

The bucket is versioned and the IAM user Mend writes as cannot delete, so this is a record that Mend itself cannot rewrite.
A file whose content has not changed is not uploaded again, so the version history only shows real changes.

This is kept apart from mend_ops.py so that boto3 stays optional: the rest of Mend runs without it.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

BUCKET_ENV = "MEND_ARCHIVE_BUCKET"
PREFIX_ENV = "MEND_ARCHIVE_PREFIX"
REGION_ENV = "MEND_ARCHIVE_REGION"
DEFAULT_PREFIX = "mend"          # infra/ lets the IAM user write under this folder only (its data_prefix variable)

_CONTENT_TYPES = {".parquet": "application/vnd.apache.parquet", ".json": "application/json", ".jsonl": "application/x-ndjson"}
# What S3 answers when the object is not there, or when the caller may read objects but not list the bucket. Either way: upload.
_NOT_THERE = {"404", "NoSuchKey", "NotFound", "403", "AccessDenied", "Forbidden"}


class ArchiveError(RuntimeError):
    """An archive step that cannot continue. The message names the problem and never contains a credential."""


@dataclass(frozen=True)
class Target:
    bucket: str
    prefix: str = DEFAULT_PREFIX
    region: str | None = None

    @classmethod
    def from_env(cls, bucket: str | None = None, prefix: str | None = None, env=os.environ) -> "Target | None":
        """None when no bucket is configured, which means archiving is switched off."""
        bucket = (bucket or env.get(BUCKET_ENV) or "").strip()
        if not bucket:
            return None
        return cls(bucket, (prefix or env.get(PREFIX_ENV) or DEFAULT_PREFIX).strip().strip("/"), (env.get(REGION_ENV) or "").strip() or None)


def export_marts(warehouse: Path, out_dir: Path) -> list[tuple[str, Path]]:
    """Every table in the analytics schema except the staging ones, as parquet files in out_dir: [(table, file)]."""
    import duckdb
    con = duckdb.connect(str(warehouse), read_only=True)
    try:
        tables = [r[0] for r in con.execute(
            "select table_name from information_schema.tables where table_schema = 'analytics' "
            "and table_name not like 'stg\\_%' escape '\\' order by table_name").fetchall()]
        files = []
        for table in tables:
            path = out_dir / f"{table}.parquet"
            quoted = '"' + table.replace('"', '""') + '"'
            con.execute(f"copy (select * from analytics.{quoted} order by all) to '{path.as_posix()}' (format parquet)")
            files.append((table, path))
        return files
    finally:
        con.close()


def _client(target: Target):
    try:
        import boto3
    except ImportError:
        raise ArchiveError("boto3 is not installed: pip install -c constraints.txt -e '.[aws]'") from None
    return boto3.client("s3", region_name=target.region)


def _problem(exc: Exception, action: str, bucket: str, key: str) -> str:
    """A short, safe description of a failed S3 call: the error code and what to check, never the raw message."""
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
    name = type(exc).__name__
    if name == "NoCredentialsError":
        return f"no AWS credentials were found, so {key} was not {action}ed: set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY"
    if name in ("EndpointConnectionError", "ConnectTimeoutError", "ReadTimeoutError"):
        return f"could not reach S3 to {action} {key}: check the network and MEND_ARCHIVE_REGION"
    hints = {"NoSuchBucket": f"there is no bucket named {bucket!r}: check MEND_ARCHIVE_BUCKET and MEND_ARCHIVE_REGION",
             "AccessDenied": "the access key is not allowed to write there: it must belong to the mend-archive-writer user, and the prefix must match",
             "InvalidAccessKeyId": "AWS does not know this access key id",
             "SignatureDoesNotMatch": "the secret access key does not match the access key id",
             "PermanentRedirect": "the bucket is in another region: set MEND_ARCHIVE_REGION"}
    return f"S3 refused to {action} {key} ({code or name}): " + hints.get(code, "see the S3 error code")


def _unchanged(s3, bucket: str, key: str, digest: str) -> bool:
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
    except Exception as exc:                                  # noqa: BLE001 - botocore's exceptions are not importable without boto3
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if code in _NOT_THERE:
            return False
        raise ArchiveError(_problem(exc, "check", bucket, key)) from None
    return head.get("Metadata", {}).get("sha256") == digest


def upload(target: Target, items: list[tuple[str, Path]], client=None) -> dict:
    """Upload [(path under the prefix, local file)], skipping files whose content is already there."""
    s3 = client or _client(target)
    uploaded = unchanged = size = 0
    for rel, path in items:
        key = f"{target.prefix}/{rel}"
        body = path.read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        if _unchanged(s3, target.bucket, key, digest):
            unchanged += 1
            continue
        try:
            s3.put_object(Bucket=target.bucket, Key=key, Body=body, ContentType=_CONTENT_TYPES.get(path.suffix, "application/octet-stream"),
                          Metadata={"sha256": digest})
        except Exception as exc:                              # noqa: BLE001
            raise ArchiveError(_problem(exc, "upload", target.bucket, key)) from None
        uploaded += 1
        size += len(body)
    return {"uploaded": uploaded, "unchanged": unchanged, "bytes": size}
