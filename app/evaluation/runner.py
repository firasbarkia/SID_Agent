"""Provisional reference comparisons and attributed human judgments stay separate."""

import json
from collections import Counter

from app.conversations.models import Answer, ContextDecision
from app.conversations.service import memory_keywords
from app.cv.models import Extraction, grounded, normalized
from app.dossier import validate_dossier
from app.evaluation.models import HumanReview, Reviews, fingerprint

REQUIRED_RATINGS = {
    "cv": ("extraction_accuracy",),
    "dossier": ("claim_support", "letter_factuality", "writing_quality"),
    "retrieval": ("sentence_support", "writing_quality"),
    "memory": ("memory_privacy",),
}


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def validate_run(corpus, run):
    if run.corpus_sha256 != fingerprint(corpus.model_dump()):
        raise ValueError("Saved run belongs to a different corpus")
    cases = {c.id: c for c in corpus.cases}
    ids = [r.case_id for r in run.results]
    if len(set(ids)) != len(ids) or set(ids) != set(cases):
        raise ValueError("Saved run must contain each corpus case exactly once")
    if set(run.provenance.prompt_sha256) != {c.kind for c in corpus.cases}:
        raise ValueError("Record a prompt fingerprint for every workflow")
    for result in run.results:
        if result.case_sha256 != fingerprint(cases[result.case_id].model_dump()):
            raise ValueError("Saved output belongs to different case inputs")
    return cases


def review_template(corpus, run):
    validate_run(corpus, run)
    return Reviews(
        schema_version=1,
        run_sha256=fingerprint(run.model_dump()),
        reviews=[
            HumanReview(case_id=r.case_id, output_sha256=fingerprint(r.model_dump()))
            for r in run.results
            if r.outcome == "success"
        ],
    )


def validated_reviews(cases, run, reviews):
    if reviews is None:
        return {}
    if reviews.run_sha256 != fingerprint(run.model_dump()):
        raise ValueError("Reviews belong to a different run")
    results = {r.case_id: r for r in run.results}
    found = {}
    for review in reviews.reviews:
        result = results.get(review.case_id)
        if review.case_id in found or result is None or result.outcome != "success":
            raise ValueError("Duplicate, unknown or non-success review")
        if review.output_sha256 != fingerprint(result.model_dump()):
            raise ValueError("Review belongs to a different saved output")
        if review.reviewer and any(
            getattr(review, key) is None for key in REQUIRED_RATINGS[cases[review.case_id].kind]
        ):
            raise ValueError("Complete the workflow's required human ratings")
        rating_fields = set(REQUIRED_RATINGS[cases[review.case_id].kind])
        for key in (
            "claim_support",
            "letter_factuality",
            "sentence_support",
            "memory_privacy",
            "extraction_accuracy",
            "writing_quality",
        ):
            if getattr(review, key) is not None and key not in rating_fields:
                raise ValueError("Human rating does not apply to this workflow")
        found[review.case_id] = review
    return found


def compare(case, result):
    metrics = {"outcome_match": result.outcome == case.expected.get("outcome", "success")}
    if result.outcome != "success":
        return metrics
    output = result.output
    if case.kind == "cv":
        extraction = Extraction.model_validate(output)
        facts, warnings = grounded(extraction, case.inputs["pages"])
        actual = {(f["field"], normalized(f["value"])) for f in facts}
        expected = {(f["field"], normalized(f["value"])) for f in case.expected["facts"]}
        metrics.update(
            matched_facts=len(actual & expected),
            extra_facts=len(actual - expected),
            missed_facts=len(expected - actual),
            expected_facts=len(expected),
            emitted_facts=len(actual),
            dropped_facts=len(extraction.facts) - len(facts),
            grounding_warnings=warnings,
        )
    elif case.kind == "dossier":
        dossier = validate_dossier(json.dumps(output), case.inputs["candidate"], case.inputs["job"])
        expected = case.expected["assessments"]
        actual = {r.requirement: r.assessment for r in dossier.evidence}
        metrics.update(
            false_supported_rows=sum(
                r.assessment == "supported" and expected.get(r.requirement) == "not_evidenced"
                for r in dossier.evidence
            ),
            supported_rows=sum(r.assessment == "supported" for r in dossier.evidence),
            missed_requirements=len(set(expected) - set(actual)),
            extra_requirements=len(set(actual) - set(expected)),
            correct_assessments=sum(actual.get(k) == v for k, v in expected.items()),
            expected_requirements=len(expected),
            duplicate_requirements=len(dossier.evidence) - len(actual),
            forbidden_letter_phrases=[
                p
                for p in case.expected.get("forbidden_letter_phrases", [])
                if normalized(p) in normalized(dossier.cover_letter)
            ],
        )
    elif case.kind == "retrieval":
        answer = Answer.model_validate(output)
        retrieved = {r["id"] for r in case.inputs["references"]}
        cited = set(answer.cited_ids)
        expected = set(case.expected["relevant_ids"])
        metrics.update(
            valid_citation_ids=bool(cited) and cited <= retrieved,
            matched_citations=len(cited & expected),
            cited_count=len(cited),
            expected_citations=len(expected),
            missed_citations=len(expected - cited),
        )
    else:
        decision = ContextDecision.model_validate(output)
        keywords = memory_keywords(
            decision.keywords, case.inputs["message"], case.inputs["previous"]
        )
        forbidden = {normalized(k) for k in case.expected["forbidden_keywords"]}
        metrics.update(
            retained_keywords=keywords,
            forbidden_keywords_retained=[k for k in keywords if normalized(k) in forbidden],
        )
    return metrics


def score(corpus, run, reviews=None):
    cases = validate_run(corpus, run)
    human = validated_reviews(cases, run, reviews)
    rows = []
    for result in run.results:
        case = cases[result.case_id]
        try:
            metrics = compare(case, result)
            validation = "accepted" if result.outcome == "success" else "no_output"
            if metrics.get("valid_citation_ids") is False:
                validation = "invalid_output"
        except (ValueError, KeyError, TypeError) as exc:
            metrics = {"outcome_match": result.outcome == case.expected.get("outcome", "success")}
            validation = "invalid_output"
            # Never copy provider output or errors (potential source text) into diagnostics.
            metrics["validation_error"] = type(exc).__name__
        review = human.get(case.id)
        rows.append(
            {
                "case_id": case.id,
                "kind": case.kind,
                "split": case.split,
                "language": case.language,
                "tags": case.tags,
                "outcome": result.outcome,
                "validation": validation,
                "provisional_metrics": metrics,
                "human_review": review.model_dump() if review and review.reviewer else None,
            }
        )
    splits = {}
    for split in ("development", "held_out"):
        selected = [r for r in rows if r["split"] == split]
        metrics = [r["provisional_metrics"] for r in selected]
        totals = Counter()
        for item in metrics:
            for key, value in item.items():
                if isinstance(value, int) and not isinstance(value, bool):
                    totals[key] += value
        completed = [r["human_review"] for r in selected if r["human_review"]]
        writing = [r["writing_quality"] for r in completed if r["writing_quality"] is not None]
        splits[split] = {
            "case_count": len(selected),
            "output_validation": dict(Counter(r["validation"] for r in selected)),
            "outcome_matches": sum(m["outcome_match"] for m in metrics),
            "provisional_totals": dict(totals),
            "provisional_fact_precision": ratio(totals["matched_facts"], totals["emitted_facts"]),
            "provisional_fact_recall": ratio(totals["matched_facts"], totals["expected_facts"]),
            "provisional_false_supported_rate": ratio(
                totals["false_supported_rows"], totals["supported_rows"]
            ),
            "provisional_assessment_accuracy": ratio(
                totals["correct_assessments"], totals["expected_requirements"]
            ),
            "provisional_citation_precision": ratio(
                totals["matched_citations"], totals["cited_count"]
            ),
            "provisional_citation_recall": ratio(
                totals["matched_citations"], totals["expected_citations"]
            ),
            "forbidden_phrase_cases": sum(bool(m.get("forbidden_letter_phrases")) for m in metrics),
            "memory_failure_cases": sum(
                bool(m.get("forbidden_keywords_retained")) for m in metrics
            ),
            "human_reviewed": len(completed),
            "human_required": sum(r["outcome"] == "success" for r in selected),
            "human_mean_writing_quality": sum(writing) / len(writing) if writing else None,
            "human_judgments": {
                key: dict(Counter(r[key] for r in completed if r[key] is not None))
                for key in (
                    "claim_support",
                    "letter_factuality",
                    "sentence_support",
                    "memory_privacy",
                    "extraction_accuracy",
                )
            },
        }
    required = sum(r["outcome"] == "success" for r in rows)
    reviewed = sum(r["human_review"] is not None for r in rows)
    return {
        "schema_version": 1,
        "run_id": run.run_id,
        "run_sha256": fingerprint(run.model_dump()),
        "corpus_sha256": run.corpus_sha256,
        "provenance": run.provenance.model_dump(),
        "reference_label_status": corpus.label_status,
        "human_review_status": "complete"
        if reviewed == required and required
        else "awaiting_review",
        "release_gate": "not_assessed",
        "splits": splits,
        "cases": rows,
    }
