"""archive: the marts and the approval records go to S3, and nothing is uploaded twice. S3 is moto's in-memory fake: no AWS account is touched."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")
duckdb = pytest.importorskip("duckdb")
from botocore.exceptions import ClientError    # noqa: E402

import mend_archive as arch    # noqa: E402
import mend_ops as ops    # noqa: E402

BUCKET = "mend-test-archive"
REPO = Path(__file__).resolve().parents[2]
SECRET = "not-a-real-key-id-0123456789"           # must never appear in an error message


@pytest.fixture(autouse=True)
def fake_aws(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", SECRET)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "not-a-real-secret")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    for name in (arch.BUCKET_ENV, arch.PREFIX_ENV, arch.REGION_ENV):
        monkeypatch.delenv(name, raising=False)
    with moto.mock_aws():
        yield


@pytest.fixture
def s3(monkeypatch):
    client = boto3.client("s3", region_name="ap-south-1")
    client.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "ap-south-1"})
    client.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})        # as infra/ does
    monkeypatch.setenv(arch.BUCKET_ENV, BUCKET)
    return client


def keys(s3) -> list[str]:
    return sorted(o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET).get("Contents", []))


def versions(s3, key: str) -> int:
    return len([v for v in s3.list_object_versions(Bucket=BUCKET, Prefix=key).get("Versions", []) if v["Key"] == key])


def test_without_a_bucket_nothing_happens(healthy):
    res = ops.archive(healthy)
    assert res["archived"] is False and arch.BUCKET_ENV in res["reason"]


def test_a_healthy_pipeline_archives_its_mart_and_the_event_log(healthy, s3, tmp_path):
    status = ops.status(healthy)
    res = ops.archive(healthy)
    day = status["mart_latest_day"]
    mart = f"mend/marts/fct_daily_revenue/as_of={day}/fct_daily_revenue.parquet"
    assert keys(s3) == ["mend/audit/events.jsonl", mart]                      # no staging table, no warehouse file, no work folders
    assert res["archived"] and res["uploaded"] == 2 and res["unchanged"] == 0 and res["mart_as_of"] == day
    local = tmp_path / "mart.parquet"
    local.write_bytes(s3.get_object(Bucket=BUCKET, Key=mart)["Body"].read())
    assert duckdb.connect().execute("select count(*) from read_parquet(?)", [str(local)]).fetchone()[0] == status["mart_days"]


def test_a_second_archive_uploads_nothing_and_adds_no_versions(healthy, s3):
    ops.archive(healthy)
    again = ops.archive(healthy)
    assert again["uploaded"] == 0 and again["unchanged"] == 2
    assert all(versions(s3, k) == 1 for k in keys(s3))


def test_approval_records_are_archived_exactly_as_written(repaired, s3):
    ops.archive(repaired)
    for name in ("proposal.json", "decision.json"):
        stored = s3.get_object(Bucket=BUCKET, Key=f"mend/audit/repairs/r0001/{name}")["Body"].read()
        assert stored == (repaired.repairs / "r0001" / name).read_bytes()
    assert json.loads(stored)["by"] == "fixture"
    assert not [k for k in keys(s3) if "/work/" in k]                         # the gate's scratch copies of the warehouse stay local


def test_a_new_day_is_a_new_partition_and_the_event_log_gains_a_version(healthy, s3):
    ops.archive(healthy)
    ops.ingest(healthy, days=1)
    assert ops.build(healthy)["status"] == "healthy"
    res = ops.archive(healthy)
    assert res["uploaded"] == 2                                              # the new day's mart and the longer event log
    assert len([k for k in keys(s3) if "/marts/" in k]) == 2
    assert versions(s3, "mend/audit/events.jsonl") == 2


def test_a_writer_that_cannot_list_the_bucket_still_archives_and_still_skips_unchanged_files(healthy, s3):
    """Without s3:ListBucket, S3 answers 403 instead of 404 for a missing object. The writer user has no list permission."""
    class Writer:
        def head_object(self, **kw):
            try:
                return s3.head_object(**kw)
            except ClientError as exc:
                if exc.response["Error"]["Code"] == "404":
                    raise ClientError({"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject") from None
                raise

        def put_object(self, **kw):
            return s3.put_object(**kw)

    assert ops.archive(healthy, client=Writer())["uploaded"] == 2
    assert ops.archive(healthy, client=Writer())["unchanged"] == 2


def test_a_missing_bucket_is_a_clear_error_that_never_shows_the_key(healthy, monkeypatch):
    monkeypatch.setenv(arch.BUCKET_ENV, "no-such-bucket")
    with pytest.raises(ops.OpsError) as err:
        ops.archive(healthy)
    assert "no bucket named" in str(err.value) and SECRET not in str(err.value)


def test_the_command_line_prints_the_result_and_the_outputs_line(healthy, s3, capsys):
    assert ops.main(["--home", str(healthy.root), "--kestra", "archive"]) == 0
    out = capsys.readouterr().out
    assert "archive: archived=True" in out and '"archived":"true"' in out


def test_the_terraform_keeps_the_bucket_private_and_the_writer_unable_to_delete_or_list():
    """The code relies on a writer that may only put and get under one folder; this pins the infrastructure to that."""
    main = (REPO / "infra" / "main.tf").read_text(encoding="utf-8")
    variables = (REPO / "infra" / "variables.tf").read_text(encoding="utf-8")
    writer = main.split('data "aws_iam_policy_document" "writer"')[1].split("\nresource ")[0]
    assert set(re.findall(r'"(s3:\w+)"', writer)) == {"s3:PutObject", "s3:GetObject"}
    assert '"${aws_s3_bucket.archive.arn}/${var.data_prefix}/*"' in writer
    default_prefix = re.search(r'variable "data_prefix".*?default\s*=\s*"([^"]+)"', variables, re.S).group(1)
    assert default_prefix == arch.DEFAULT_PREFIX
    block = main.split('resource "aws_s3_bucket_public_access_block"')[1].split("\n}")[0]
    assert len(re.findall(r"=\s*true", block)) == 4
    assert 'status = "Enabled"' in main.split('resource "aws_s3_bucket_versioning"')[1].split("\n}")[0]
    assert "aws:SecureTransport" in main
