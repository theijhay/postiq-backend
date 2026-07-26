import numpy as np
import pytest

from api.v1.services.matching_service import (
    EMBEDDING_DIM,
    NoFaceDetected,
    cosine_similarity,
    decode_embedding,
    decode_probe_b64,
    embedding_extractor,
    encode_embedding,
    encode_probe_b64,
)
from api.utils.settings import settings
from tests.faceutils import PERSON_A, PERSON_B, frames_of

SFACE_SAME_IDENTITY = 0.363  # OpenCV zoo's published threshold


def test_extractor_returns_unit_vector():
    vec = embedding_extractor.extract(frames_of(PERSON_A))
    assert vec.shape == (EMBEDDING_DIM,)
    assert np.isclose(np.linalg.norm(vec), 1.0, atol=1e-5)


def test_same_person_matches_across_bursts():
    a1 = embedding_extractor.extract(frames_of(PERSON_A, seed=1))
    a2 = embedding_extractor.extract(frames_of(PERSON_A, seed=2))
    similarity = cosine_similarity(a1, a2)
    assert similarity >= settings.MATCH_THRESHOLD, f"same person scored {similarity}"


def test_different_people_do_not_match():
    a = embedding_extractor.extract(frames_of(PERSON_A))
    b = embedding_extractor.extract(frames_of(PERSON_B))
    similarity = cosine_similarity(a, b)
    assert similarity < SFACE_SAME_IDENTITY, f"different people scored {similarity}"


def test_no_face_raises():
    noise = [np.random.default_rng(i).integers(0, 255, 32, dtype=np.uint8).tobytes() for i in range(3)]
    with pytest.raises(NoFaceDetected):
        embedding_extractor.extract(noise)


def test_encode_decode_round_trip():
    vec = embedding_extractor.extract(frames_of(PERSON_A))
    assert np.allclose(decode_embedding(encode_embedding(vec)), vec)
    assert np.allclose(decode_probe_b64(encode_probe_b64(vec)), vec)


def test_cosine_identity_and_orthogonal():
    v = np.zeros(4, dtype=np.float32)
    v[0] = 1.0
    w = np.zeros(4, dtype=np.float32)
    w[1] = 1.0
    assert cosine_similarity(v, v) == pytest.approx(1.0)
    assert cosine_similarity(v, w) == pytest.approx(0.0)


def test_probe_rejects_garbage():
    with pytest.raises(ValueError):
        decode_probe_b64("not-base64!!!")
    with pytest.raises(ValueError):
        decode_probe_b64("AAAA")  # wrong dimension
