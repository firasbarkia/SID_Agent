"""Deterministic compatibility rubric compat-v1."""

import math
import os

import numpy as np
import pytest

from app.matching.aliases import AliasTable
from app.matching.rubric import LABEL, Rubric, template_justification

DIMS = 8


def unit(index):
    vector = np.zeros(DIMS, dtype=np.float32)
    vector[index] = 1.0
    return vector


def at(cosine):
    """A unit vector whose cosine with unit(0) is exactly `cosine`."""
    vector = np.zeros(DIMS, dtype=np.float32)
    vector[0], vector[1] = cosine, math.sqrt(1 - cosine**2)
    return vector


@pytest.fixture
def rubric():
    return Rubric(AliasTable.load(), "test-fingerprint")


def test_alias_skill_counts_as_listed(rubric):
    result = rubric.score([("ReactJS", unit(0))], [("React.js", unit(3))], [])
    row = result.criteria[0]
    assert (row.level, row.points, row.similarity, row.evidence) == ("listed", 0.8, 1.0, "React.js")
    assert result.percent == 80 and result.gaps == []


def test_statement_ngram_demonstrates_implied_requirement(rubric):
    statement = "Développement de composants ReactJS pour un site e-commerce"
    result = rubric.score([("Développement Frontend", unit(0))], [], [(statement, unit(4))])
    assert result.criteria[0].level == "demonstrated" and result.criteria[0].evidence == statement


@pytest.mark.parametrize(
    "skill,statement,level",
    [
        (0.9, None, "listed"),
        (None, 0.85, "demonstrated"),
        (0.6, None, "related"),
        (None, 0.55, "related"),
        (0.3, 0.45, "missing"),
    ],
)
def test_semantic_similarity_levels(rubric, skill, statement, level):
    skills = [("Some listed skill", at(skill))] if skill is not None else []
    statements = [("Some unrelated sentence", at(statement))] if statement is not None else []
    row = rubric.score([("Requirement", unit(0))], skills, statements).criteria[0]
    assert row.level == level
    assert (row.evidence is None) == (level == "missing")


def test_percent_mean_of_points_and_gaps(rubric):
    result = rubric.score(
        [("Django", unit(0)), ("Git", unit(1)), ("Kubernetes", unit(2))],
        [("GitHub", unit(5))],
        [("Built a reservation app with Django", unit(6))],
    )
    assert [r.level for r in result.criteria] == ["demonstrated", "listed", "missing"]
    assert result.percent == 60 and result.gaps == ["Kubernetes"]
    assert result.label == LABEL and result.rubric_version == "compat-v1"
    assert result.model_fingerprint == "test-fingerprint"


def test_single_letter_ngrams_never_match(rubric):
    result = rubric.score([("C", unit(0))], [], [("C'est un projet web", unit(5))])
    assert result.criteria[0].level == "missing"


def test_no_criteria_has_no_score(rubric):
    assert rubric.score([], [("Python", unit(0))], []) is None


def test_template_justification_uses_computed_values(rubric):
    result = rubric.score([("Django", unit(0)), ("Kubernetes", unit(2))], [("Django", unit(0))], [])
    french = template_justification(result, "fr")
    english = template_justification(result, "en")
    assert french.startswith("Matching 40 %") and "Kubernetes" in french and "Django" in french
    assert english.startswith("Matching 40%") and "missing: Kubernetes" in english


@pytest.mark.skipif(
    not os.environ.get("SID_TEST_EMBEDDING_DIRECTORY"),
    reason="Set SID_TEST_EMBEDDING_DIRECTORY to run the real-model calibration check",
)
def test_real_model_calibration():
    from app.knowledge.embedding import LocalEmbedder

    model = LocalEmbedder(os.environ["SID_TEST_EMBEDDING_DIRECTORY"], 1)
    rubric = Rubric(AliasTable.load(), model.fingerprint)

    def phrases(*texts):
        return list(zip(texts, (np.asarray(v) for v in model.encode(list(texts))), strict=True))

    def level(requirement, skills=(), statements=()):
        result = rubric.score(phrases(requirement), phrases(*skills), phrases(*statements))
        return result.criteria[0].level

    # The specification's own example: ReactJS counts toward "Développement Frontend".
    assert level("Développement Frontend", skills=["ReactJS"]) == "listed"
    assert level("Gestion de projet", skills=["Project management"]) == "listed"
    assert level("Statistiques", skills=["Data analysis"]) == "related"
    assert level("Mobile development", statements=["Application Flutter de covoiturage"]) == (
        "demonstrated"
    )
    assert level("Python", skills=["Comptabilité"]) == "missing"
    assert level("Kubernetes", skills=["Communication"]) == "missing"
