import math

import pytest

from app.providers.errors import InvalidProviderResponse
from app.providers.vectors import normalize_embedding


def test_scales_to_unit_length() -> None:
    vector = normalize_embedding([3.0, 4.0], 2)
    assert vector == pytest.approx([0.6, 0.8])
    assert math.isclose(math.hypot(*vector), 1.0)


def test_unit_vector_is_unchanged() -> None:
    assert normalize_embedding([0.0, 1.0, 0.0], 3) == [0.0, 1.0, 0.0]


def test_tiny_but_valid_vector_normalises() -> None:
    vector = normalize_embedding([1e-30, 1e-30], 2)
    assert math.isclose(math.hypot(*vector), 1.0)


@pytest.mark.parametrize(
    ("values", "match"),
    [
        (None, "empty vector"),
        ([1.0, 2.0], "3-dimension"),
        ([[1.0, 2.0, 3.0]], "3-dimension"),
        ([1.0, math.nan, 1.0], "NaN or infinite"),
        ([1.0, math.inf, 1.0], "NaN or infinite"),
        ([0.0, 0.0, 0.0], "zero length"),
        (["a", "b", "c"], "non-numeric"),
    ],
)
def test_rejects_invalid_vectors(values: object, match: str) -> None:
    with pytest.raises(InvalidProviderResponse, match=match):
        normalize_embedding(values, 3)  # type: ignore[arg-type]
