"""Write a dependency inventory without local paths, credentials or environment data."""

import importlib.metadata
import json
import subprocess

python = []
for dist in importlib.metadata.distributions():
    if dist.metadata["Name"] == "trace-hash-sdk":
        continue
    python.append(
        {
            "name": dist.metadata["Name"],
            "version": dist.version,
            "license_expression": dist.metadata.get("License-Expression"),
            "license_classifiers": [
                c for c in dist.metadata.get_all("Classifier", []) if c.startswith("License ::")
            ],
        }
    )
metadata = json.loads(subprocess.check_output(["cargo", "metadata", "--locked", "--format-version", "1"]))
rust = [
    {"name": p["name"], "version": p["version"], "license_expression": p["license"]}
    for p in metadata["packages"]
]
print(
    json.dumps(
        {
            "format": "trace-dependency-inventory-v1",
            "python": sorted(python, key=lambda p: p["name"].lower()),
            "rust": sorted(rust, key=lambda p: p["name"]),
        },
        indent=2,
    )
)
