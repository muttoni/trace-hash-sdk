import io
import wave

import numpy as np
from PIL import Image
from trace_hash import Config, Limits, compare


def image_bytes(size=256, fmt="PNG"):
    rng = np.random.default_rng(12345)
    array = rng.integers(0, 256, (size, size, 3), dtype=np.uint8)
    out = io.BytesIO()
    Image.fromarray(array).save(out, format=fmt)
    return out.getvalue()


def test_pdq_and_metadata_invariance(fp):
    from PIL.PngImagePlugin import PngInfo

    data = image_bytes()
    out = io.BytesIO()
    image = Image.open(io.BytesIO(data))
    info = PngInfo()
    info.add_text("private", "TOP_SECRET")
    image.save(out, format="PNG", pnginfo=info)
    a, b = fp(data, "first.png")[0], fp(out.getvalue(), "metadata.png")[0]
    assert a["features"] == b["features"]
    result = compare(a, b)
    assert result["class"] == "uncertain" and result["proposed_class"] == "near_duplicate"
    assert len(a["features"][0]["payload"]["hash"]) == 64
    assert len(a["features"][1]["payload"]["regions"]) == 10


def test_image_pixel_budget(fp):
    row = fp(image_bytes(), "large.png", Config(limits=Limits(max_pixels=10000)))[0]
    assert row["status"] == "partial" and row["exact_digest"]


def wav(samples, rate=11025):
    out = io.BytesIO()
    with wave.open(out, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes((samples * 20000).astype("<i2").tobytes())
    return out.getvalue()


def test_audio_stream_and_silence(fp):
    t = np.arange(11025 * 15) / 11025
    x = (np.sin(2 * np.pi * (500 * t + 5 * t * t)) + np.sin(2 * np.pi * (1200 * t + 13 * t * t))) / 3
    row = fp(wav(x), "audio.wav")[0]
    assert row["status"] == "processed", row["issues"]
    payload = row["features"][0]["payload"]
    assert payload["samples"] == len(x)
    assert len(payload["landmarks"]) > 100
    silence = fp(wav(np.zeros(11025)), "silence.wav")[0]
    assert silence["features"][0]["payload"]["low_information"]


def test_pdf_scanned_page(fp):
    image = Image.open(io.BytesIO(image_bytes()))
    out = io.BytesIO()
    image.save(out, format="PDF")
    rows = fp(out.getvalue(), "scan.pdf")
    assert "ocr_not_performed" in rows[0]["issues"]
    assert any(row["modality"] == "image" for row in rows[1:])


def test_video_sampling(tmp_path, fp):
    import av

    path = tmp_path / "generated.mp4"
    with av.open(str(path), "w") as container:
        stream = container.add_stream("mpeg4", rate=2)
        stream.width, stream.height, stream.pix_fmt = 128, 128, "yuv420p"
        rng = np.random.default_rng(12)
        for i in range(24):
            frame = av.VideoFrame.from_ndarray(
                rng.integers(0, 255, (128, 128, 3), dtype=np.uint8), format="rgb24"
            )
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    row = fp(path.read_bytes(), "movie.mp4")[0]
    assert row["features"], row["issues"]
    assert "sampled_video" in row["issues"]
    frames = next(f["payload"]["frames"] for f in row["features"] if f["kind"] == "video")
    assert len(frames) == 12
    assert [f["time_ms"] for f in frames] == list(range(0, 12000, 1000))
