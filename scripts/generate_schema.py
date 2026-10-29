"""Generate the public schema and the identical packaged schema."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def obj(properties, required=None):
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties) if required is None else required,
    }


def arr(items, maximum=500000):
    return {"type": "array", "items": items, "maxItems": maximum}


integer = {"type": "integer", "minimum": 0}
boolean = {"type": "boolean"}
hex64 = {"type": "string", "pattern": "^[a-f0-9]{64}$"}
hex16 = {"type": "string", "pattern": "^[a-f0-9]{16}$"}
uuid = {"type": "string", "pattern": "^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$"}
quality = {"type": "integer", "minimum": 0, "maximum": 100}
anchor = obj({"hash": hex16, "position": integer})
payloads = {
    "text": obj(
        {
            "canonical_digest": hex64,
            "minhash": arr(hex16, 128),
            "anchors": arr(anchor),
            "units": integer,
            "distinct_shingles": integer,
            "low_information": boolean,
            "truncated": boolean,
        }
    ),
    "image": obj(
        {"hash": hex64, "quality": quality, "width": integer, "height": integer, "low_information": boolean}
    ),
    "image_regions": obj(
        {
            "regions": arr(
                obj(
                    {
                        "hash": hex64,
                        "quality": quality,
                        "box": {"type": "array", "items": integer, "minItems": 4, "maxItems": 4},
                    }
                ),
                10,
            )
        }
    ),
    "audio": obj(
        {
            "landmarks": arr(anchor),
            "samples": integer,
            "sample_rate": {"const": 11025},
            "hop": {"const": 512},
            "low_information": boolean,
            "truncated": boolean,
        }
    ),
    "video": obj(
        {
            "frames": arr(obj({"hash": hex64, "quality": quality, "time_ms": integer})),
            "sample_period_ms": {"const": 1000},
            "low_information": boolean,
        }
    ),
}
features = {
    "oneOf": [
        obj(
            {
                "kind": {"const": kind},
                "profile": {"type": "string", "pattern": "^trace\\.[a-zA-Z0-9.+_-]+$", "maxLength": 200},
                "experimental": {"const": True},
                "payload": payload,
            }
        )
        for kind, payload in payloads.items()
    ]
}
issues = [
    "field_mapping_required",
    "invalid_record",
    "input_size_limit",
    "record_size_limit",
    "expansion_limit",
    "record_limit",
    "depth_limit",
    "page_limit",
    "pixel_limit",
    "duration_limit",
    "feature_limit",
    "manifest_size_limit",
    "spool_size_limit",
    "multi_frame_image",
    "sampled_video",
    "fuzzy_unsupported",
    "optional_dependency_missing",
    "decode_failed",
    "source_changed",
    "source_read_failed",
    "worker_terminated",
    "symlink_skipped",
    "non_regular_entry_skipped",
    "document_extraction_experimental",
    "document_coverage_limited",
    "ocr_not_performed",
    "child_coverage_incomplete",
]
schema = obj(
    {
        "schema_version": {"const": "1.0"},
        "dataset_id": uuid,
        "item_id": uuid,
        "parent_id": {"anyOf": [uuid, {"type": "null"}]},
        "unit": {"enum": ["file", "record", "segment"]},
        "position": {"anyOf": [integer, {"type": "null"}]},
        "modality": {
            "enum": ["binary", "text", "code", "image", "audio", "video", "record", "document", "container"]
        },
        "byte_length": integer,
        "exact_digest": {"anyOf": [hex64, {"type": "null"}]},
        "exact_algorithm": {"const": "blake3-256"},
        "exact_basis": {"enum": ["raw_bytes", "canonical_record", "derived_content"]},
        "status": {"enum": ["processed", "partial", "unsupported", "failed"]},
        "coverage": {"enum": ["complete", "partial", "none"]},
        "issues": arr({"enum": issues}, 32),
        "features": arr(features, 128),
        "implementation": {"const": "trace-hash-sdk/0.1.0"},
    }
)
schema.update({"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "urn:trace:manifest:1.0"})
for path in (ROOT / "schemas/manifest.schema.json", ROOT / "python/trace_hash/manifest.schema.json"):
    path.write_text(json.dumps(schema, indent=2) + "\n")
