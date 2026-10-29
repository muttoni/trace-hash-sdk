"""Strict upload envelopes. Local source metadata never belongs in these objects."""

from __future__ import annotations

import importlib.metadata
import json
import uuid
from dataclasses import asdict, dataclass, field

VERSION = "0.1.0"
POLICY = "trace.conservative.v0"
BOUNDARIES = [
    "No detected overlap applies only to the checked registry, profiles, and snapshot; it does not establish global exclusivity.",
    "Submission and contributor assertions do not prove training use, ownership, or licensing rights.",
    "Fingerprints can disclose membership and similarity information; keeping content local is not a secrecy guarantee.",
]


@dataclass(frozen=True)
class Limits:
    max_input_bytes: int = 1 << 40
    max_record_bytes: int = 8 << 20
    max_expanded_bytes: int = 1 << 30
    max_spool_bytes: int = 1 << 30
    max_records: int = 10000
    max_depth: int = 4
    max_pixels: int = 40_000_000
    max_pages: int = 1000
    max_duration_seconds: int = 3600
    max_features: int = 500_000
    max_manifest_bytes: int = 128 << 20
    memory_bytes: int = 4 << 30
    timeout_seconds: int = 600

    def __post_init__(self):
        if any(type(v) is not int or v <= 0 for v in asdict(self).values()):
            raise ValueError("limits must be positive integers")
        if self.max_features > 500000 or self.max_manifest_bytes > 128 << 20:
            raise ValueError("limits exceed the v1 manifest schema capacity")


@dataclass(frozen=True)
class Config:
    limits: Limits = field(default_factory=Limits)
    # JSON pointers map explicitly to content modalities; media strings are base64.
    fields: dict[str, str] = field(default_factory=dict)
    s3_endpoint: str | None = None

    def __post_init__(self):
        if any(
            not k.startswith("/") or v not in ("text", "code", "image", "audio", "video")
            for k, v in self.fields.items()
        ):
            raise ValueError("fields must map JSON pointers to explicit content modalities")
        if self.s3_endpoint:
            from urllib.parse import urlsplit

            u = urlsplit(self.s3_endpoint)
            if u.scheme != "https" or not u.netloc or u.username or u.password:
                raise ValueError("S3 endpoints must use HTTPS without embedded credentials")


def config_dict(config: Config) -> dict:
    return asdict(config)


def config_from_dict(data: dict) -> Config:
    return Config(
        limits=Limits(**data["limits"]), fields=data.get("fields", {}), s3_endpoint=data.get("s3_endpoint")
    )


def opaque_id() -> str:
    return str(uuid.uuid4())


def child_id(parent: str, index: int) -> str:
    return str(uuid.uuid5(uuid.UUID(parent), str(index)))


def runtime_profile(base: str, *packages: str) -> str:
    from . import _native

    versions = {p: importlib.metadata.version(p) for p in packages}
    if "av" in packages:
        import av

        versions["ffmpeg"] = json.dumps(av.library_versions, sort_keys=True)
    suffix = _native.digest_bytes(json.dumps(versions, sort_keys=True).encode())[:16]
    return base + "+" + suffix


def envelope(
    dataset_id: str,
    item_id: str,
    *,
    parent_id=None,
    modality="binary",
    size=0,
    digest=None,
    status="processed",
    issues=None,
    features=None,
    coverage="complete",
    unit="file",
    position=None,
    exact_basis="raw_bytes",
) -> dict:
    return {
        "schema_version": "1.0",
        "dataset_id": dataset_id,
        "item_id": item_id,
        "parent_id": parent_id,
        "unit": unit,
        "position": position,
        "modality": modality,
        "byte_length": size,
        "exact_digest": digest,
        "exact_algorithm": "blake3-256",
        "exact_basis": exact_basis,
        "status": status,
        "coverage": coverage,
        "issues": issues or [],
        "features": features or [],
        "implementation": "trace-hash-sdk/" + VERSION,
    }


def feature(kind: str, profile: str, payload: dict) -> dict:
    return {"kind": kind, "profile": profile, "experimental": True, "payload": payload}


class LimitExceeded(Exception):
    pass
