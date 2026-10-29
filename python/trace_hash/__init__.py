"""Trace SDK: local content processing; uploads require explicit TraceClient calls."""

from .client import TraceClient
from .matching import compare
from .model import VERSION as __version__
from .model import Config, Limits
from .sdk import fingerprint_file, fingerprint_record, fingerprint_stream, iter_fingerprints

__all__ = [
    "Config",
    "Limits",
    "fingerprint_file",
    "fingerprint_stream",
    "fingerprint_record",
    "iter_fingerprints",
    "compare",
    "TraceClient",
    "__version__",
]
