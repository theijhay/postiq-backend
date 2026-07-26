"""Turns a single portrait into a realistic capture burst for tests/e2e.

A real camera burst has micro-motion and exposure drift between frames; we
simulate that with small translations and brightness changes so the real
face engine sees plausibly-live sequences.
"""

from pathlib import Path

import cv2
import numpy as np

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PERSON_A = FIXTURES / "person_a.jpg"
PERSON_B = FIXTURES / "person_b.jpg"


def frames_of(image_path: Path, count: int = 4, seed: int = 0) -> list[bytes]:
    image = cv2.imread(str(image_path))
    if image is None:
        raise FileNotFoundError(f"fixture not readable: {image_path}")
    rng = np.random.default_rng(seed)
    h, w = image.shape[:2]
    frames: list[bytes] = []
    for i in range(count):
        # micro-motion: shift by a few pixels; exposure drift: small gain
        dx, dy = (float(v) for v in rng.uniform(2.0, 6.0, size=2) * (1 if i % 2 else -1))
        matrix = np.float32([[1, 0, dx], [0, 1, dy]])
        shifted = cv2.warpAffine(image, matrix, (w, h), borderMode=cv2.BORDER_REFLECT)
        gain = 1.0 + float(rng.uniform(-0.05, 0.05))
        adjusted = np.clip(shifted.astype(np.float32) * gain, 0, 255).astype(np.uint8)
        ok, encoded = cv2.imencode(".jpg", adjusted, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if not ok:
            raise RuntimeError("jpeg encode failed")
        frames.append(encoded.tobytes())
    return frames


def still_frames(image_path: Path, count: int = 4) -> list[bytes]:
    """Byte-identical frames — what a photo pinned to the lens produces."""
    ok, encoded = cv2.imencode(".jpg", cv2.imread(str(image_path)))
    if not ok:
        raise RuntimeError("jpeg encode failed")
    return [encoded.tobytes()] * count
