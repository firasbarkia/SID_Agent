import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.evaluation.models import Corpus, HumanReview, SavedRun, fingerprint
from app.evaluation.runner import review_template, score

DATA = Path(__file__).resolve().parents[1] / "data/evaluation"


@pytest.fixture
def corpus():
    return Corpus.model_validate_json((DATA / "corpus-v1.json").read_text("utf-8"))


@pytest.fixture
def saved_run():
    return SavedRun.model_validate_json((DATA / "synthetic-baseline-v1.json").read_text("utf-8"))


def test_synthetic_baseline_exposes_validator_blind_spots(corpus, saved_run):
    report = score(corpus, saved_run)
    cases = {r["case_id"]: r for r in report["cases"]}
    for id in ("dossier-en-unrelated", "dossier-fr-negation", "dossier-fr-conflict"):
        assert cases[id]["validation"] == "accepted"
        assert cases[id]["provisional_metrics"]["false_supported_rows"] == 1
    assert cases["dossier-en-missing"]["provisional_metrics"]["missed_requirements"] == 1
    assert cases["dossier-en-company"]["provisional_metrics"]["forbidden_letter_phrases"]
    assert cases["cv-en-injection"]["provisional_metrics"]["extra_facts"] == 1
    assert cases["cv-fr-negation"]["provisional_metrics"]["extra_facts"] == 1
    assert cases["rag-en-unknown"]["validation"] == "invalid_output"
    assert cases["rag-fr-sentence"]["validation"] == "accepted"
    assert cases["rag-fr-sentence"]["provisional_metrics"]["matched_citations"] == 0
    assert cases["memory-en-name"]["provisional_metrics"]["forbidden_keywords_retained"] == [
        "Alice Example"
    ]
    assert cases["memory-en-negation"]["provisional_metrics"]["forbidden_keywords_retained"] == [
        "Tunis"
    ]
    assert cases["memory-fr-email"]["provisional_metrics"]["forbidden_keywords_retained"] == []
    assert cases["cv-en-project"]["provisional_metrics"]["extra_facts"] == 0
    assert cases["dossier-fr-alias"]["provisional_metrics"]["false_supported_rows"] == 0
    assert report["release_gate"] == "not_assessed"
    assert report["human_review_status"] == "awaiting_review"
    for split in report["splits"].values():
        assert split["case_count"] == 10
        assert split["human_reviewed"] == 0
        assert split["human_mean_writing_quality"] is None


def test_blank_review_is_not_a_human_label(corpus, saved_run):
    template = review_template(corpus, saved_run)
    assert len(template.reviews) == 18  # Failure outcomes do not have output to review.
    report = score(corpus, saved_run, template)
    assert report["human_review_status"] == "awaiting_review"
    assert all(r["human_review"] is None for r in report["cases"])


def test_human_scores_are_separate_and_bound_to_exact_output(corpus, saved_run):
    template = review_template(corpus, saved_run)
    review = next(r for r in template.reviews if r.case_id == "dossier-en-unrelated")
    # Synthetic ratings exercise aggregation; they are not shipped as human evidence.
    review.reviewer = "test-reviewer"
    review.claim_support = "unsupported"
    review.letter_factuality = "unsupported"
    review.writing_quality = 2
    report = score(corpus, saved_run, template)
    assert report["splits"]["development"]["human_reviewed"] == 1
    assert report["splits"]["development"]["human_judgments"]["claim_support"] == {"unsupported": 1}
    assert report["splits"]["held_out"]["human_reviewed"] == 0
    assert report["human_review_status"] == "awaiting_review"
    review.output_sha256 = "0" * 64
    with pytest.raises(ValueError, match="different saved output"):
        score(corpus, saved_run, template)


@pytest.mark.parametrize("case", ["corpus", "case", "duplicate", "missing", "unknown", "prompt"])
def test_saved_output_provenance_and_coverage_are_required(corpus, saved_run, case):
    if case == "corpus":
        saved_run.corpus_sha256 = "0" * 64
    elif case == "case":
        saved_run.results[0].case_sha256 = "0" * 64
    elif case == "duplicate":
        saved_run.results.append(saved_run.results[0])
    elif case == "missing":
        saved_run.results.pop()
    elif case == "unknown":
        saved_run.results[0].case_id = "unknown-case"
    else:
        saved_run.provenance.prompt_sha256.pop("cv")
    with pytest.raises(ValueError):
        score(corpus, saved_run)


def test_schema_failure_and_unexpected_outcome_are_reported(corpus, saved_run):
    saved_run.results[0].output = {"facts": "not a fact list with private text"}
    refused = next(r for r in saved_run.results if r.case_id == "dossier-en-refusal")
    refused.outcome = "unavailable"
    report = score(corpus, saved_run)
    row = report["cases"][0]
    assert row["validation"] == "invalid_output"
    assert row["provisional_metrics"]["validation_error"] == "ValidationError"
    assert "private text" not in json.dumps(report)
    assert report["splits"]["development"]["outcome_matches"] == 9


def test_scores_require_attribution_and_complete_relevant_fields(corpus, saved_run):
    with pytest.raises(ValidationError, match="named reviewer"):
        HumanReview(case_id="example", output_sha256="0" * 64, writing_quality=5)
    template = review_template(corpus, saved_run)
    review = next(r for r in template.reviews if r.case_id == "memory-en-name")
    review.reviewer = "test-reviewer"
    with pytest.raises(ValueError, match="required human ratings"):
        score(corpus, saved_run, template)
    review.memory_privacy = "identity_leak"
    review.letter_factuality = "faithful"
    with pytest.raises(ValueError, match="does not apply"):
        score(corpus, saved_run, template)


def test_duplicate_reviews_and_cross_run_reviews_are_rejected(corpus, saved_run):
    template = review_template(corpus, saved_run)
    template.reviews.append(template.reviews[0])
    with pytest.raises(ValueError, match="Duplicate"):
        score(corpus, saved_run, template)
    template.reviews.pop()
    template.run_sha256 = "0" * 64
    with pytest.raises(ValueError, match="different run"):
        score(corpus, saved_run, template)


def test_corpus_and_provenance_do_not_accept_malformed_expectations_or_secrets(corpus, saved_run):
    data = corpus.model_dump()
    data["cases"][0]["expected"]["facts"] = None
    with pytest.raises(ValidationError, match="reference facts"):
        Corpus.model_validate(data)
    data = saved_run.model_dump()
    data["provenance"]["configuration"]["api_key"] = "do-not-record-credentials"
    with pytest.raises(ValidationError):
        SavedRun.model_validate(data)


def test_offline_cli_writes_report_and_review_artifacts(tmp_path):
    output = tmp_path / "report.json"
    markdown = tmp_path / "report.md"
    base = [sys.executable, "-m", "app.evaluation.cli"]
    subprocess.run(
        base
        + [
            "score",
            "--outputs",
            str(DATA / "synthetic-baseline-v1.json"),
            "--out",
            str(output),
            "--markdown",
            str(markdown),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    report = json.loads(output.read_text("utf-8"))
    assert report["human_review_status"] == "awaiting_review"
    assert "not assessed" in markdown.read_text("utf-8")
    review = tmp_path / "reviews.json"
    subprocess.run(
        base
        + [
            "init-review",
            "--outputs",
            str(DATA / "synthetic-baseline-v1.json"),
            "--out",
            str(review),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    assert json.loads(review.read_text())["run_sha256"] == report["run_sha256"]
    run = json.loads((DATA / "synthetic-baseline-v1.json").read_text("utf-8"))
    run["provenance"]["configuration"]["api_key"] = "private-value-must-not-be-printed"
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps(run), encoding="utf-8")
    result = subprocess.run(
        base + ["score", "--outputs", str(invalid), "--out", str(output)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert "private-value-must-not-be-printed" not in result.stderr


def test_saved_run_canonical_hash_survives_json_reformatting(saved_run):
    formatted = SavedRun.model_validate_json(json.dumps(saved_run.model_dump(), indent=4))
    assert fingerprint(formatted.model_dump()) == fingerprint(saved_run.model_dump())
