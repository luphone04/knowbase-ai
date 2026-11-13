"""The Asia Cement follow-up extension (Experiment 006) next to the unchanged benchmark.

Only the extension and the parent's public development split are read here. The held-out
split is private: it is compared by the blind check alone (counts only, never content),
and only where the private file is installed.
"""

import json
import uuid
from collections import Counter
from typing import Any

import pytest
from pydantic import ValidationError

import app.api.v1  # noqa: F401  (loads before app.evaluation.service, as the app does)
from app.config import Settings
from app.evaluation.blind_check import heldout_duplicates
from app.evaluation.dataset import (
    ASIA_CEMENT_DEV_DATASET,
    ASIA_CEMENT_ID,
    ASIA_FOLLOWUPS_DATASET,
    ASIA_FOLLOWUPS_ID,
    DEFAULT_DATASET,
    NORTHSTAR_ID,
    DatasetError,
    dataset_for,
    dataset_id_for,
    load_asia_cement_dev,
    load_asia_followups,
    load_dataset,
    parse_asia_cement,
)
from app.evaluation.service import _reference
from app.retrieval.followup import is_follow_up
from tests.fakes.asia_cement import PRIVATE, PRIVATE_REASON, full_data

# The files every historical run recorded (evaluation_runs.dataset_sha256): unchanged.
ASIA_CEMENT_SHA256 = "c74fae181d09f25d04f07854ea84982fa747422e0e3358a34f3d5ab978868cf9"  # private
NORTHSTAR_SHA256 = "dc7a0fd8b65cd4697d0401b12d99a280ecd3d98604b6779e299f932206bdc981"
# asia-cement-followups 1.0.0 as frozen after the final blind held-out check.
FOLLOWUPS_SHA256 = "54830bc6c625257a75f7fd1c5867fd7d3b036231ee5d48fedd68f94f9347eafd"

FOLLOWUPS = load_asia_followups()
PARENT = load_asia_cement_dev()  # the public development split of the parent benchmark
private = pytest.mark.skipif(PRIVATE is None, reason=PRIVATE_REASON)
COPIES = ("AC-017", "AC-019")
ANNOTATIONS = ("follow_up_type", "origin")  # the only fields a copy adds


def raw(path: Any = ASIA_FOLLOWUPS_DATASET) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_bytes())
    return data


def entry(data: dict[str, Any], key: str) -> dict[str, Any]:
    found: dict[str, Any] = next(q for q in data["questions"] if q["id"] == key)
    return found


def question(key: str) -> Any:
    return next(q for q in FOLLOWUPS.questions if q.key == key)


# --- the original datasets are unchanged ----------------------------------------------------


def test_northstar_is_byte_for_byte_what_historical_runs_used() -> None:
    assert load_dataset().sha256 == NORTHSTAR_SHA256


@private
def test_the_private_benchmark_is_byte_for_byte_what_historical_runs_used() -> None:
    assert PRIVATE is not None
    assert PRIVATE.sha256 == ASIA_CEMENT_SHA256
    assert (PRIVATE.name, PRIVATE.version) == (ASIA_CEMENT_ID, "1.0.0")
    assert len(PRIVATE.questions) == 24 and len(PRIVATE.run_questions) == 16
    assert PRIVATE.excluded_questions == 8
    assert [q.key for q in PRIVATE.questions] == [f"AC-{n:03d}" for n in range(1, 25)]
    # The generalized parser adds nothing to the parent's questions.
    assert all(q.follow_up_type is None for q in PRIVATE.questions)
    assert all(q.corroborating_evidence == () for q in PRIVATE.questions)
    assert all(
        (q.previous_question is not None) == (q.category == "follow_up") for q in PRIVATE.questions
    )


def test_the_parent_development_split_has_no_extension_only_fields() -> None:
    assert all(q.follow_up_type is None for q in PARENT.questions)
    assert all(q.corroborating_evidence == () for q in PARENT.questions)


def test_the_parent_format_still_refuses_extension_only_shapes() -> None:
    data = full_data()  # the parent's shape, with synthetic held-out questions
    entry(data, "AC-001")["id"] = "FU-001"
    with pytest.raises(DatasetError, match="not a question id asia-cement-realworld uses"):
        parse_asia_cement(json.dumps(data).encode())
    data = full_data()
    for q in data["questions"]:
        q["split"] = "dev"
    with pytest.raises(DatasetError, match="heldout split is empty"):
        parse_asia_cement(json.dumps(data).encode())


# --- the extension ---------------------------------------------------------------------------


NEW = [q for q in FOLLOWUPS.questions if q.key.startswith("FU-")]  # newly written
NEW_ANSWERABLE = [q for q in NEW if q.answerable]  # the population for criteria A-C
NEW_PRONOUN_OR_CONTEXT = [
    q for q in NEW_ANSWERABLE if q.follow_up_type in ("pronoun", "context_dependent")
]


def test_the_frozen_extension_has_20_development_follow_ups() -> None:
    assert (FOLLOWUPS.name, FOLLOWUPS.version) == (ASIA_FOLLOWUPS_ID, "1.0.0")
    assert FOLLOWUPS.relevance == "passage" and FOLLOWUPS.run_split == "dev"
    keys = [q.key for q in FOLLOWUPS.questions]
    # The 2 inherited diagnostic cases, then 18 of the 20 written FU- ids (2 excluded by the
    # blind held-out check before freezing; ids are never reused or renumbered).
    assert keys[:2] == list(COPIES) and keys[2:] == sorted(keys[2:])
    assert len(NEW) == 18 and {q.key for q in NEW} < {f"FU-{n:03d}" for n in range(1, 21)}
    assert len(FOLLOWUPS.run_questions) == 20 and FOLLOWUPS.excluded_questions == 0
    assert (FOLLOWUPS.answerable, FOLLOWUPS.unanswerable) == (18, 2)
    assert all(q.previous_question and q.follow_up_type for q in FOLLOWUPS.questions)
    assert len(NEW_ANSWERABLE) == 16
    assert Counter(q.follow_up_type for q in NEW_ANSWERABLE) == {
        "pronoun": 5,
        "context_dependent": 4,
        "topic_change": 4,
        "self_contained": 3,
    }
    abstain = [q for q in FOLLOWUPS.questions if not q.answerable]
    assert [(q.key, q.category, q.evidence) for q in abstain] == [
        ("FU-010", "absent_detail", ()),
        ("FU-020", "absent_detail", ()),
    ]
    assert sum(len(q.evidence) for q in FOLLOWUPS.questions) == 29  # mandatory quotations
    assert sum(len(q.evidence) for q in NEW) == 25
    # Same four SHA-pinned PDFs as the parent: the indexed workspaces need no re-ingestion.
    assert FOLLOWUPS.source_hashes == PARENT.source_hashes
    assert FOLLOWUPS.source_files == PARENT.source_files


def test_ac_017_and_ac_019_are_verbatim_copies_of_the_development_questions() -> None:
    parent, extension = raw(ASIA_CEMENT_DEV_DATASET), raw()
    for key in COPIES:
        copy = {k: v for k, v in entry(extension, key).items() if k not in ANNOTATIONS}
        assert copy == entry(parent, key)
        assert entry(parent, key)["split"] == "dev"
    assert entry(extension, "AC-017")["follow_up_type"] == "context_dependent"
    assert entry(extension, "AC-019")["follow_up_type"] == "pronoun"


def test_fu_013_scores_mandatory_evidence_only() -> None:
    fu013 = question("FU-013")
    assert fu013.expected_source_keys == ("acp",)
    assert [(e.source_key, e.page) for e in fu013.evidence] == [("acp", 13)]
    assert [(e.source_key, e.page) for e in fu013.corroborating_evidence] == [("bpcc", 7)]
    assert fu013.category == "follow_up"  # not counted as multi-source
    assert not any("Business Partner" in fact for fact in fu013.required_facts)
    # Stored apart for reviewers: the scored evidence list never includes it.
    reference = _reference(fu013)
    assert [e["source_key"] for e in reference["evidence"]] == ["acp"]
    assert [e["source_key"] for e in reference["corroborating_evidence"]] == ["bpcc"]
    # No other question has corroborating evidence.
    assert [q.key for q in FOLLOWUPS.questions if q.corroborating_evidence] == ["FU-013"]
    assert "corroborating_evidence" not in _reference(question("FU-019"))


def test_fu_015_keeps_the_2019_supplier_code_and_the_2025_partner_code_apart() -> None:
    fu015 = question("FU-015")
    assert [(e.source_key, e.page) for e in fu015.evidence] == [("bpcc", 6), ("terms", 3)]
    assert set(fu015.expected_source_keys) == {"bpcc", "terms"}
    facts = " ".join(fu015.required_facts)
    assert "Oct 2025" in facts and "May 2019" in facts
    assert "legally equivalent" in facts and "same instrument" in facts
    answer = fu015.expected_answer or ""
    assert "do not say that the 2019 Supplier Code of Conduct is the 2025" in answer
    assert "Con-duct" in fu015.evidence[1].quote  # as printed in the PDF


def test_evidence_counts_by_document_and_multi_source() -> None:
    answerable = [q for q in FOLLOWUPS.questions if q.answerable]
    multi = [q.key for q in answerable if len(q.expected_source_keys) > 1]
    assert multi == ["FU-015", "FU-019"]
    for q in answerable:
        assert {e.source_key for e in q.evidence} == set(q.expected_source_keys), q.key


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda d: entry(d, "FU-001").update(split="heldout"), "has no heldout split"),
        (lambda d: entry(d, "FU-001").pop("follow_up_type"), "follow-up with a follow_up_type"),
        (lambda d: entry(d, "FU-001").update(id="XX-001"), "id"),
        (
            lambda d: entry(d, "FU-010").update(
                corroborating_evidence=entry(d, "FU-013")["corroborating_evidence"]
            ),
            "abstention question has no sources or evidence",
        ),
        (
            lambda d: entry(d, "FU-013").update(
                corroborating_evidence=entry(d, "FU-013")["evidence"]
            ),
            "repeats a scored quotation",
        ),
        (lambda d: entry(d, "FU-001").update(follow_up_type="vague"), "follow_up_type"),
        (lambda d: entry(d, "FU-001").update(extra="field"), "Extra inputs"),
    ],
)
def test_broken_extensions_are_rejected(edit: Any, message: str) -> None:
    data = raw()
    edit(data)
    with pytest.raises(DatasetError, match=message):
        parse_asia_cement(json.dumps(data).encode())


def test_each_loader_accepts_only_its_own_dataset() -> None:
    with pytest.raises(DatasetError, match="Expected the asia-cement-realworld benchmark"):
        parse_asia_cement(ASIA_FOLLOWUPS_DATASET.read_bytes(), name=ASIA_CEMENT_ID)
    with pytest.raises(DatasetError, match="Expected the asia-cement-followups benchmark"):
        parse_asia_cement(ASIA_CEMENT_DEV_DATASET.read_bytes(), name=ASIA_FOLLOWUPS_ID)
    with pytest.raises(DatasetError):
        parse_asia_cement(DEFAULT_DATASET.read_bytes())


# --- selection -----------------------------------------------------------------------------


def settings(**env: Any) -> Settings:
    return Settings(
        _env_file=None,
        database_url="postgresql+psycopg://u:p@localhost/db",
        jwt_secret="j" * 40,
        app_secret_key="a" * 40,
        **env,
    )


def test_both_experiment_workspaces_can_use_the_extension_others_keep_theirs() -> None:
    paragraph, section, original, other = (uuid.uuid4() for _ in range(4))
    parsed = settings(
        evaluation_workspace_datasets={
            str(paragraph): ASIA_FOLLOWUPS_ID,
            str(section): ASIA_FOLLOWUPS_ID,
            str(original): ASIA_CEMENT_ID,
        }
    )
    assignments = parsed.evaluation_workspace_datasets
    assert dataset_for(paragraph, assignments).name == ASIA_FOLLOWUPS_ID
    assert dataset_for(section, assignments).name == ASIA_FOLLOWUPS_ID
    assert dataset_id_for(original, assignments) == ASIA_CEMENT_ID  # its file is private
    assert dataset_id_for(other, assignments) == NORTHSTAR_ID
    with pytest.raises(ValidationError):
        settings(evaluation_workspace_datasets={str(other): "asia-cement-followups-v2"})


# --- the blind held-out duplicate check --------------------------------------------------------


@private
def test_the_extension_is_frozen_only_if_the_blind_check_passes() -> None:
    assert PRIVATE is not None
    report = heldout_duplicates(FOLLOWUPS, PRIVATE)  # counts only
    assert (report.candidate_questions, report.held_out_questions) == (20, 8)
    frozen = "NOT FROZEN" not in raw()["status"]
    assert report.passed or not frozen, report


def test_the_frozen_file_is_pinned() -> None:
    # Any edit needs a new version (ensure_questions refuses a changed file).
    assert raw()["status"].startswith("FROZEN")
    assert FOLLOWUPS.sha256 == FOLLOWUPS_SHA256


# --- pre-registration (Experiment 006) --------------------------------------------------------


def test_criteria_a_to_c_are_registered_on_the_new_questions_only() -> None:
    assert len(NEW_PRONOUN_OR_CONTEXT) == 9  # criterion C's subgroup
    # Under the production follow-up heuristic these have identical joined and standalone
    # queries, so merged equals joined for them by construction.
    same = [q for q in NEW_ANSWERABLE if not is_follow_up(q.question)]
    assert len(same) == 7 and sum(q in NEW_PRONOUN_OR_CONTEXT for q in same) == 1
    assert all(is_follow_up(q.question) for q in FOLLOWUPS.questions if q.key in COPIES)
    guidance = raw()["scoring_guidance"]
    registered = guidance["experiment_006_preregistered"]
    for text in (
        "the 16 newly written answerable follow-ups (FU- ids) only",
        "never counted toward A-C",
        ">= +3 questions on section-v1 and >= 0 on paragraph-v1",
        "= 0 on the new pronoun/context-dependent questions",
        "<= 1 across all 16 new questions",
        "on the 9 new pronoun/context-dependent answerable questions",
        "MRR is reported separately",
        "regression tests on asia-cement-realworld",
        "7 of the 16 new answerable questions",
    ):
        assert text in registered, text
    assert "2 of the 20 newly written questions were removed" in guidance["exclusions"]
