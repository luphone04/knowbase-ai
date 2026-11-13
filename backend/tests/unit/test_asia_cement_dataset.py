"""The Asia Cement benchmark: its own strict format, its split, and per-workspace selection.

The full benchmark (`asia-cement-realworld`, with the sealed held-out split) is private and
not in the repository. Its public development split (`asia-cement-realworld-dev`) is, and
split handling is tested on a full-shaped benchmark with synthetic held-out questions
(tests/fakes/asia_cement.py). Tests of the real private file run only where it's installed.
"""

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.evaluation.dataset import (
    ASIA_CEMENT_DATASET,
    ASIA_CEMENT_DEV_DATASET,
    ASIA_CEMENT_DEV_ID,
    ASIA_CEMENT_ID,
    DEFAULT_DATASET,
    NORTHSTAR_ID,
    DatasetError,
    DatasetUnavailable,
    dataset_for,
    dataset_id_for,
    load_asia_cement,
    load_asia_cement_dev,
    parse_asia_cement,
    parse_dataset,
)
from tests.fakes.asia_cement import DEV, FULL, PRIVATE, PRIVATE_REASON, full_bytes, full_data

# The private file every historical asia-cement-realworld run recorded (dataset_sha256).
PRIVATE_SHA256 = "c74fae181d09f25d04f07854ea84982fa747422e0e3358a34f3d5ab978868cf9"
BENCHMARK = Path(__file__).resolve().parents[3] / "asia-cement" / "asia-cement-benchmark-v1.json"
# The four PDFs as recorded in the benchmark (QA_REPORT.md), pinned here as well.
DOCUMENTS = {
    "cobc": ("ACC_Code of Business Conduct 2024_0.pdf", "2f943365e84a"),
    "acp": ("Anti-Corruption Policy.pdf", "69b68d1160ee"),
    "bpcc": ("Business Partner Code of Conduct_for publication.pdf", "81533b2eae25"),
    "terms": ("general_purchasing_terms_and_conditions.pdf", "cc14e770a022"),
}
private = pytest.mark.skipif(PRIVATE is None, reason=PRIVATE_REASON)


def parse(data: dict[str, Any]) -> None:
    parse_asia_cement(full_bytes(data))


def test_the_public_development_split_loads() -> None:
    dataset = load_asia_cement_dev()
    assert (dataset.name, dataset.version) == (ASIA_CEMENT_DEV_ID, "1.0.0")
    assert len(dataset.questions) == 16 and all(q.split == "dev" for q in dataset.questions)
    assert (dataset.answerable, dataset.unanswerable) == (13, 3)
    assert dataset.relevance == "passage" and dataset.run_split == "dev"
    assert len(dataset.run_questions) == 16 and dataset.excluded_questions == 0
    assert sum(len(q.evidence) for q in dataset.questions) == 28
    assert all(e.page is not None for q in dataset.questions for e in q.evidence)
    for key, (name, prefix) in DOCUMENTS.items():
        assert dataset.file_name(key) == name
        assert dataset.source_hashes[key].startswith(prefix)
    # Known cash/gift-card failures stay in the development split (QA report).
    assert {"AC-001", "AC-017"} <= {q.key for q in dataset.questions}
    # It records where it comes from: the private file's checksum, nothing of its held-out.
    extends = json.loads(ASIA_CEMENT_DEV_DATASET.read_bytes())["extends"]
    assert (extends["dataset"], extends["sha256"]) == (ASIA_CEMENT_ID, PRIVATE_SHA256)


def test_a_full_benchmark_runs_its_development_split_only() -> None:
    # Synthetic held-out questions: stored with the dataset, never run.
    assert (FULL.name, len(FULL.questions)) == (ASIA_CEMENT_ID, 18)
    assert [q.key for q in FULL.run_questions] == [q.key for q in DEV.questions]
    assert FULL.excluded_questions == 2
    assert {q.split for q in FULL.questions if q not in FULL.run_questions} == {"heldout"}


@private
def test_the_private_benchmark_is_unchanged_and_its_dev_split_is_the_public_file() -> None:
    assert PRIVATE is not None
    assert PRIVATE.sha256 == PRIVATE_SHA256  # what every historical run recorded
    assert (PRIVATE.name, PRIVATE.version) == (ASIA_CEMENT_ID, "1.0.0")
    assert len(PRIVATE.questions) == 24 and (PRIVATE.answerable, PRIVATE.unanswerable) == (19, 5)
    assert len(PRIVATE.run_questions) == 16 and PRIVATE.excluded_questions == 8
    assert sum(len(q.evidence) for q in PRIVATE.questions) == 40
    private_raw = json.loads(ASIA_CEMENT_DATASET.read_bytes())
    public_raw = json.loads(ASIA_CEMENT_DEV_DATASET.read_bytes())
    dev = [q for q in private_raw["questions"] if q["split"] == "dev"]
    assert public_raw["questions"] == dev  # verbatim, nothing else
    assert public_raw["documents"] == private_raw["documents"]


@pytest.mark.skipif(
    PRIVATE is None or not BENCHMARK.is_file(), reason="the unpacked benchmark is private"
)
def test_the_private_file_is_the_benchmark_byte_for_byte() -> None:
    assert ASIA_CEMENT_DATASET.read_bytes() == BENCHMARK.read_bytes()
    assert load_asia_cement().sha256 == hashlib.sha256(BENCHMARK.read_bytes()).hexdigest()


def test_the_full_benchmark_is_reported_unavailable_where_it_is_not_installed(
    tmp_path: Path,
) -> None:
    with pytest.raises(DatasetUnavailable, match="private and isn't installed here"):
        load_asia_cement(tmp_path / "asia-cement-realworld-v1.json")


def test_the_formats_are_never_mixed() -> None:
    with pytest.raises(DatasetError):
        parse_dataset(ASIA_CEMENT_DEV_DATASET.read_bytes())  # Northstar's rules are unchanged
    with pytest.raises(DatasetError):
        parse_asia_cement(DEFAULT_DATASET.read_bytes())
    with pytest.raises(DatasetError, match="Expected the asia-cement-realworld benchmark"):
        parse_asia_cement(ASIA_CEMENT_DEV_DATASET.read_bytes(), name=ASIA_CEMENT_ID)
    data = json.loads(ASIA_CEMENT_DEV_DATASET.read_bytes())
    data["questions"][0]["split"] = "heldout"  # the public split never holds held-out ones
    with pytest.raises(DatasetError, match="has no heldout split"):
        parse_asia_cement(json.dumps(data).encode())


def question(data: dict[str, Any], key: str) -> dict[str, Any]:
    found: dict[str, Any] = next(q for q in data["questions"] if q["id"] == key)
    return found


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda d: question(d, "AC-001").update(split="test"), "split"),
        (lambda d: question(d, "AC-001")["evidence"][0].update(source_key="nope"), "unknown"),
        (lambda d: question(d, "AC-001").update(expected_behavior="abstain"), "contradicts"),
        (lambda d: question(d, "AC-017").update(previous_question=None), "follow_up"),
        (
            lambda d: question(d, "AC-020").update(expected_source_keys=["cobc"]),
            "no sources or evidence",
        ),
        (lambda d: question(d, "AC-002").update(id="AC-001"), "duplicate id"),
        (lambda d: question(d, "AC-001").update(extra="field"), "Extra inputs"),
        (lambda d: d["documents"][0].update(sha256="ABC"), "SHA-256"),
        (
            lambda d: [q.update(split="dev") for q in d["questions"] if q["split"] == "heldout"],
            "heldout split is empty",
        ),
        (lambda d: question(d, "AC-001")["evidence"][0].update(pdf_page=0), "pdf_page"),
    ],
)
def test_broken_benchmarks_are_rejected(edit: Any, message: str) -> None:
    data = full_data()
    edit(data)
    with pytest.raises(DatasetError, match=message):
        parse(data)


def test_selection_is_by_workspace_id_and_defaults_to_northstar() -> None:
    asia, dev, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    assignments = {asia: ASIA_CEMENT_ID, dev: ASIA_CEMENT_DEV_ID}
    assert dataset_id_for(asia, assignments) == ASIA_CEMENT_ID
    assert dataset_for(dev, assignments).name == ASIA_CEMENT_DEV_ID
    assert dataset_id_for(other, assignments) == NORTHSTAR_ID
    assert dataset_for(other, {}).name == "northstar-demo"


def settings(**env: Any) -> Settings:
    return Settings(
        _env_file=None,
        database_url="postgresql+psycopg://u:p@localhost/db",
        jwt_secret="j" * 40,
        app_secret_key="a" * 40,
        **env,
    )


def test_the_setting_defaults_to_nobody_and_accepts_known_datasets_only() -> None:
    assert settings().evaluation_workspace_datasets == {}
    workspace = uuid.uuid4()
    for dataset in (ASIA_CEMENT_ID, ASIA_CEMENT_DEV_ID):
        parsed = settings(evaluation_workspace_datasets={str(workspace): dataset})
        assert parsed.evaluation_workspace_datasets == {workspace: dataset}
    with pytest.raises(ValidationError):
        settings(evaluation_workspace_datasets={str(workspace): "something-else"})
    with pytest.raises(ValidationError):  # a workspace id, never an account email
        settings(evaluation_workspace_datasets={"someone@example.com": ASIA_CEMENT_ID})
