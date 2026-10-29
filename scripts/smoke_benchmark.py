"""Small synthetic engineering workload; explicitly NOT the release pilot."""

import argparse
import json
import platform
import tempfile
import wave
from pathlib import Path


def main(output):
    import av
    import numpy as np
    from PIL import Image
    from trace_hash.cli import main as cli

    with tempfile.TemporaryDirectory(prefix="trace-smoke-") as temporary:
        root = Path(temporary)
        for i in range(32):
            text = " ".join(f"record{i}word{j}" for j in range(8000))
            (root / f"text-{i}.txt").write_text(text)
        rng = np.random.default_rng(20261028)
        for i in range(8):
            image = Image.fromarray(rng.integers(0, 255, (512, 512, 3), dtype=np.uint8))
            image.save(root / f"image-{i}.png")
        image.save(root / "scanned.pdf", format="PDF")
        for i in range(2):
            t = np.arange(11025 * 20) / 11025
            pcm = (np.sin(2 * np.pi * (300 * t + (i + 4) * t * t)) / 3 * 20000).astype("<i2")
            with wave.open(str(root / f"audio-{i}.wav"), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(11025)
                out.writeframes(pcm.tobytes())
        with av.open(str(root / "video.mp4"), "w") as out:
            stream = out.add_stream("mpeg4", rate=2)
            stream.width, stream.height, stream.pix_fmt = 128, 128, "yuv420p"
            for _ in range(24):
                frame = av.VideoFrame.from_ndarray(
                    rng.integers(0, 255, (128, 128, 3), dtype=np.uint8), format="rgb24"
                )
                for packet in stream.encode(frame):
                    out.mux(packet)
            for packet in stream.encode():
                out.mux(packet)
        status = cli(["benchmark", str(root), "--workers", "1", "2", "4", "8", "--output", str(output)])
        if status:
            raise SystemExit(status)
    report = json.loads(output.read_text())
    report["workload"] = {
        "kind": "synthetic_smoke",
        "seed": 20261028,
        "files": 44,
        "note": "Small OS-cache-warm workload including process startup; not representative pilot evidence.",
    }
    report["host"] = {
        "machine": platform.machine(),
        "os": platform.system(),
        "python": platform.python_version(),
    }
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            [
                {
                    "workers": r["workers"],
                    "seconds": r["metrics"]["elapsed_seconds"],
                    "input_bytes": r["metrics"]["input_bytes"],
                    "counts": r["counts"],
                }
                for r in report["measurements"]
            ]
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("benchmarks/smoke.json"))
    main(parser.parse_args().output)
