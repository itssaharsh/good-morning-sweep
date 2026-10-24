"""Pixels without a model: hashes, blur, brightness, thumbnails and one frame from each video."""
import hashlib
import io
import os
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageOps

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif"}
VIDEO_EXT = {".mp4", ".3gp", ".mkv", ".mov", ".webm"}


def is_video(path):
    return Path(path).suffix.lower() in VIDEO_EXT


def is_image(path):
    return Path(path).suffix.lower() in IMAGE_EXT


def sha1(path, chunk=1 << 20):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def ffmpeg_bin():
    for c in (os.environ.get("SWEEP_FFMPEG"), str(Path.home() / ".local/opt/ffmpeg/bin/ffmpeg"), shutil.which("ffmpeg")):
        if c and Path(c).exists():
            return c
    return None


def video_frame(path, at_seconds=1.0):
    """One frame as a PIL image, or None when the video can't be read."""
    ff = ffmpeg_bin()
    if not ff:
        return None
    cmd = [ff, "-v", "error", "-ss", str(at_seconds), "-i", str(path), "-frames:v", "1",
           "-vf", "scale='min(768,iw)':-2", "-f", "image2pipe", "-vcodec", "mjpeg", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=60).stdout
        if not out and at_seconds > 0:
            out = subprocess.run(cmd[:4] + ["0"] + cmd[5:], capture_output=True, timeout=60).stdout
        return Image.open(io.BytesIO(out)).convert("RGB") if out else None
    except Exception:
        return None


def open_picture(path, max_side=None):
    """The picture (or a video's frame) as RGB, upright, or None. max_side lets JPEG decode at a smaller size."""
    try:
        if is_video(path):
            return video_frame(path)
        im = Image.open(path)
        if max_side and im.format == "JPEG":
            im.draft("RGB", (max_side, max_side))
        im = ImageOps.exif_transpose(im)
        if getattr(im, "is_animated", False):
            im.seek(0)
        return im.convert("RGB")
    except Exception:
        return None


def dhash(im, size=8):
    """64-bit difference hash as 16 hex chars; near-identical pictures differ in a few bits."""
    g = im.convert("L").resize((size + 1, size), Image.LANCZOS)
    px = list(g.tobytes())
    bits = 0
    for row in range(size):
        for col in range(size):
            left, right = px[row * (size + 1) + col], px[row * (size + 1) + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return f"{bits:016x}"


def blur_and_dark(im):
    """(sharpness, brightness): variance of a Laplacian on a 256 px greyscale copy, and mean brightness."""
    import numpy as np
    g = im.convert("L")
    g.thumbnail((256, 256))
    a = np.asarray(g, dtype=np.float32)
    if a.shape[0] < 3 or a.shape[1] < 3:
        return 0.0, float(a.mean()) if a.size else 0.0
    lap = (a[1:-1, :-2] + a[1:-1, 2:] + a[:-2, 1:-1] + a[2:, 1:-1] - 4 * a[1:-1, 1:-1])
    return float(lap.var()), float(a.mean())


def jpeg_bytes(im, max_side=768, quality=85):
    im = im.copy()
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def thumbnail(path, cache_dir, file_id, side=320):
    """A small JPEG for the review page, cached on disk."""
    cache = Path(cache_dir) / f"{file_id}.jpg"
    if cache.exists():
        return cache.read_bytes()
    im = open_picture(path)
    if im is None:
        return None
    data = jpeg_bytes(im, max_side=side, quality=80)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(data)
    return data
