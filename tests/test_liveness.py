import numpy as np

from api.utils.settings import settings
from api.v1.services.liveness_service import (
    SCORE_NO_FACE,
    SCORE_NO_MOTION,
    SCORE_TOO_FEW_FRAMES,
    liveness_checker,
)
from tests.faceutils import PERSON_A, frames_of, still_frames


def test_live_burst_passes():
    score = liveness_checker.score(frames_of(PERSON_A))
    assert score >= settings.LIVENESS_THRESHOLD


def test_too_few_frames_fail():
    assert liveness_checker.score(frames_of(PERSON_A, count=2)) == SCORE_TOO_FEW_FRAMES


def test_frozen_image_fails():
    """A photo pinned to the lens produces identical frames — rejected."""
    assert liveness_checker.score(still_frames(PERSON_A)) == SCORE_NO_MOTION


def test_no_face_fails():
    noise = [np.random.default_rng(i).integers(0, 255, 64, dtype=np.uint8).tobytes() for i in range(4)]
    assert liveness_checker.score(noise) == SCORE_NO_FACE
