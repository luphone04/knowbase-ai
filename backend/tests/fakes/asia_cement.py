"""Asia Cement benchmark data for tests, without the private held-out split.

- `DEV`: the public development split (`asia-cement-realworld-dev`, 16 questions).
- `FULL`: an `asia-cement-realworld`-shaped benchmark made of those 16 development
  questions plus two **synthetic** held-out questions (made-up text that is in no PDF),
  so split handling (held-out questions stored but never run) is tested everywhere,
  CI included, without the private file.
- `PRIVATE`: the real full benchmark where its private file is installed (local
  machines), else None. Tests of the real file are skipped without it; CI never has it.
"""

import json
from typing import Any

from app.evaluation.dataset import (
    ASIA_CEMENT_DATASET,
    ASIA_CEMENT_DEV_DATASET,
    ASIA_CEMENT_ID,
    Dataset,
    load_asia_cement,
    load_asia_cement_dev,
    parse_asia_cement,
)

DEV = load_asia_cement_dev()
PRIVATE: Dataset | None = load_asia_cement() if ASIA_CEMENT_DATASET.is_file() else None
PRIVATE_REASON = "the full Asia Cement benchmark is private (not in the repository)"

SYNTHETIC_HELD_OUT: list[dict[str, Any]] = [
    {
        "id": "AC-901",
        "category": "direct",
        "split": "heldout",
        "question": "Synthetic held-out question: what does the test-only clause require?",
        "previous_question": None,
        "expected_behavior": "answer",
        "reference_answer": "The test-only clause requires nothing; it exists for tests.",
        "required_facts": ["Test-only clause"],
        "expected_source_keys": ["cobc"],
        "evidence": [
            {
                "source_key": "cobc",
                "pdf_page": 3,
                "section": "Synthetic",
                "quote": "This synthetic test-only clause appears in no Asia Cement document.",
            }
        ],
        "notes": "Synthetic: made up for tests, never part of the benchmark.",
    },
    {
        "id": "AC-902",
        "category": "out_of_scope",
        "split": "heldout",
        "question": "Synthetic held-out question: what is the test-only revenue figure?",
        "previous_question": None,
        "expected_behavior": "abstain",
        "reference_answer": "Not stated.",
        "required_facts": [],
        "expected_source_keys": [],
        "evidence": [],
        "notes": "Synthetic: made up for tests, never part of the benchmark.",
    },
]


def full_data() -> dict[str, Any]:
    """The development split, reshaped as the full benchmark with synthetic held-out."""
    data: dict[str, Any] = json.loads(ASIA_CEMENT_DEV_DATASET.read_bytes())
    data.pop("extends", None)
    data.update(name=ASIA_CEMENT_ID, version="synthetic-1", status="test data")
    data["questions"] = [*data["questions"], *json.loads(json.dumps(SYNTHETIC_HELD_OUT))]
    return data


def full_bytes(data: dict[str, Any] | None = None) -> bytes:
    return json.dumps(data if data is not None else full_data()).encode()


FULL = parse_asia_cement(full_bytes(), name=ASIA_CEMENT_ID)
