import io
import uuid
from datetime import datetime, timezone

import boto3
from botocore.response import StreamingBody
from botocore.stub import Stubber
from trace_hash import Config, _native
from trace_hash.sdk import _s3


def test_s3_version_pinning_and_digest(monkeypatch, text):
    s3 = boto3.client("s3", region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test")
    data = text.encode()
    with Stubber(s3) as stub:
        stub.add_response(
            "head_object",
            {"ContentLength": len(data), "ETag": '"not-a-digest"', "VersionId": "version-1"},
            {"Bucket": "bucket", "Key": "a.txt"},
        )
        stub.add_response(
            "get_object",
            {"Body": StreamingBody(io.BytesIO(data), len(data)), "ContentLength": len(data)},
            {"Bucket": "bucket", "Key": "a.txt", "VersionId": "version-1"},
        )
        monkeypatch.setattr(boto3, "client", lambda *a, **kw: s3)
        rows = _s3("s3://bucket/a.txt", str(uuid.uuid4()), str(uuid.uuid4()), Config())
        assert rows[0]["exact_digest"] == _native.digest_bytes(data)
        stub.assert_no_pending_responses()


def test_unversioned_source_change_invalidates(monkeypatch):
    s3 = boto3.client("s3", region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test")
    args = {"Bucket": "bucket", "Key": "a.txt"}
    stamp = datetime.now(timezone.utc)
    with Stubber(s3) as stub:
        stub.add_response("head_object", {"ContentLength": 3, "ETag": '"old"', "LastModified": stamp}, args)
        stub.add_response(
            "get_object", {"Body": StreamingBody(io.BytesIO(b"abc"), 3)}, {**args, "IfMatch": '"old"'}
        )
        stub.add_response("head_object", {"ContentLength": 3, "ETag": '"new"', "LastModified": stamp}, args)
        monkeypatch.setattr(boto3, "client", lambda *a, **kw: s3)
        row = _s3("s3://bucket/a.txt", str(uuid.uuid4()), str(uuid.uuid4()), Config())[0]
        assert row["issues"] == ["source_changed"] and row["exact_digest"] is None
