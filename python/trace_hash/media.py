"""Optional native decoders, called inside resource-limited worker processes."""

import json

from . import _native
from .model import LimitExceeded, feature, runtime_profile


def pdq(rgb):
    import numpy as np
    import pdqhash

    vector, quality = pdqhash.compute(np.ascontiguousarray(rgb, dtype=np.uint8))
    return np.packbits(vector.astype(np.uint8), bitorder="big").tobytes().hex(), int(quality)


def image_features(path, config):
    from PIL import Image

    previous = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = config.limits.max_pixels
    try:
        return _image_features(path, config)
    except Image.DecompressionBombError:
        raise LimitExceeded("pixel_limit") from None
    finally:
        Image.MAX_IMAGE_PIXELS = previous


def _image_features(path, config):
    import numpy as np
    from PIL import Image, ImageOps

    with Image.open(path) as image:
        if image.width * image.height > config.limits.max_pixels:
            raise LimitExceeded("pixel_limit")
        if getattr(image, "n_frames", 1) != 1:
            raise LimitExceeded("multi_frame_image")
        image = ImageOps.exif_transpose(image)
        # Composite alpha onto a specified white background, retain encoded RGB (no implicit ICC conversion).
        rgba = image.convert("RGBA")
        rgb = Image.alpha_composite(Image.new("RGBA", rgba.size, "white"), rgba).convert("RGB")
        array = np.asarray(rgb)
        digest, quality = pdq(array)
        regions = []
        for size in (0.5, 0.75):
            for x, y in (
                (0, 0),
                (1 - size, 0),
                (0, 1 - size),
                (1 - size, 1 - size),
                ((1 - size) / 2, (1 - size) / 2),
            ):
                box = (
                    int(x * rgb.width),
                    int(y * rgb.height),
                    int((x + size) * rgb.width),
                    int((y + size) * rgb.height),
                )
                h, q = pdq(np.asarray(rgb.crop(box)))
                regions.append({"hash": h, "quality": q, "box": list(box)})
        profile = runtime_profile("trace.image.pdq256-white-exif.v0", "Pillow", "pdqhash", "numpy")
        return [
            feature(
                "image",
                profile,
                {
                    "hash": digest,
                    "quality": quality,
                    "width": rgb.width,
                    "height": rgb.height,
                    "low_information": quality < 50,
                },
            ),
            feature(
                "image_regions",
                profile.replace("pdq256-white-exif", "pdq-regions10-white-exif"),
                {"regions": regions},
            ),
        ]


def audio_video_features(path, config, video=False):
    import av

    audio = _native.AudioHasher(config.limits.max_features)
    frames, issues = [], []
    next_frame = 0.0
    samples = 0
    seen_audio = False
    with av.open(str(path), options={"protocol_whitelist": "file,pipe"}) as container:
        streams = list(container.streams.audio[:1])
        if video:
            streams += list(container.streams.video[:1])
        if not streams:
            raise ValueError("missing_media_stream")
        resampler = av.AudioResampler(format="fltp", layout="mono", rate=11025)
        for packet in container.demux(streams):
            for frame in packet.decode():
                if isinstance(frame, av.AudioFrame):
                    seen_audio = True
                    for out in resampler.resample(frame):
                        pcm = out.to_ndarray().reshape(-1)
                        samples += len(pcm)
                        if samples > config.limits.max_duration_seconds * 11025:
                            raise LimitExceeded("duration_limit")
                        audio.push(pcm.tolist())
                elif video:
                    if frame.width * frame.height > config.limits.max_pixels:
                        raise LimitExceeded("pixel_limit")
                    if frame.time is None or frame.time < 0:
                        raise ValueError("missing_media_timestamp")
                    time = float(frame.time)
                    if time > config.limits.max_duration_seconds:
                        raise LimitExceeded("duration_limit")
                    if time >= next_frame:
                        h, q = pdq(frame.to_ndarray(format="rgb24"))
                        frames.append({"hash": h, "quality": q, "time_ms": round(time * 1000)})
                        next_frame = int(time) + 1.0
                        if len(frames) >= config.limits.max_features:
                            raise LimitExceeded("feature_limit")
        if seen_audio:
            for out in resampler.resample(None):
                audio.push(out.to_ndarray().reshape(-1).tolist())
    results = []
    if seen_audio:
        p = json.loads(audio.finish())
        base = p.pop("profile")
        if p["truncated"]:
            issues.append("feature_limit")
        results.append(feature("audio", runtime_profile(base, "av"), p))
    if video:
        if not frames:
            raise ValueError("missing_media_stream")
        results.append(
            feature(
                "video",
                runtime_profile("trace.video.pdq-1fps.v0", "av", "pdqhash", "numpy"),
                {
                    "frames": frames,
                    "sample_period_ms": 1000,
                    "low_information": sum(f["quality"] >= 50 for f in frames) < 10,
                },
            )
        )
        issues.append("sampled_video")
    return results, issues
