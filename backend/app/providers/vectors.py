"""Embedding validation and L2 normalisation.

gemini-embedding-001 returns unit-length vectors only at its native 3072
dimensions. Truncated 768-dimension vectors come back with a norm of about
0.59 (measured in M0) and must be normalised manually (SPEC.md §2.2). Once
every stored and query vector has length 1, cosine similarity is just a dot
product, and pgvector's cosine distance compares directions, not lengths.
"""

import math
from collections.abc import Sequence

import numpy as np

from app.providers.errors import InvalidProviderResponse

_NORM_TOLERANCE = 1e-6


def normalize_embedding(values: Sequence[float] | None, dimensions: int) -> list[float]:
    """Validate one raw embedding and return it scaled to unit length."""
    if values is None:
        raise InvalidProviderResponse("The embedding response contained an empty vector.")
    try:
        vector = np.asarray(values, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise InvalidProviderResponse("The embedding contained non-numeric values.") from exc

    if vector.shape != (dimensions,):
        raise InvalidProviderResponse(
            f"Expected a {dimensions}-dimension embedding, got shape {vector.shape}."
        )
    if not np.all(np.isfinite(vector)):
        raise InvalidProviderResponse("The embedding contained NaN or infinite values.")

    norm = float(np.linalg.norm(vector))
    if not math.isfinite(norm) or norm == 0.0:
        raise InvalidProviderResponse("The embedding has zero length and cannot be normalised.")

    unit = vector / norm
    if abs(float(np.linalg.norm(unit)) - 1.0) > _NORM_TOLERANCE:
        raise InvalidProviderResponse("The embedding could not be normalised to unit length.")
    return [float(x) for x in unit]
