"""tests/test_triage_schema.py — unit tests for TriageResult Pydantic validation."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.incidents import Category, Severity, TriageResult


class TestTriageResultValidation:
    def test_valid_result_parses(self):
        data = {
            "severity": "HIGH",
            "category": "APPLICATION_ERROR",
            "is_actionable": True,
            "summary": "User lookup returns None.",
            "root_cause_hypothesis": "Missing null check before accessing user.email.",
            "confidence": 0.91,
            "evidence": ["AttributeError in users.py", "Object is None"],
        }
        result = TriageResult.model_validate(data)
        assert result.severity == Severity.HIGH
        assert result.category == Category.APPLICATION_ERROR
        assert result.is_actionable is True
        assert result.confidence == pytest.approx(0.91)
        assert len(result.evidence) == 2

    def test_invalid_severity_raises(self):
        data = {
            "severity": "EXTREME",  # invalid
            "category": "APPLICATION_ERROR",
            "is_actionable": True,
            "summary": "x",
            "root_cause_hypothesis": "y",
            "confidence": 0.5,
        }
        with pytest.raises(ValidationError):
            TriageResult.model_validate(data)

    def test_invalid_category_raises(self):
        data = {
            "severity": "HIGH",
            "category": "ALIEN_INVASION",  # invalid
            "is_actionable": True,
            "summary": "x",
            "root_cause_hypothesis": "y",
            "confidence": 0.5,
        }
        with pytest.raises(ValidationError):
            TriageResult.model_validate(data)

    def test_confidence_above_1_raises(self):
        data = {
            "severity": "HIGH",
            "category": "APPLICATION_ERROR",
            "is_actionable": True,
            "summary": "x",
            "root_cause_hypothesis": "y",
            "confidence": 1.5,  # invalid
        }
        with pytest.raises(ValidationError):
            TriageResult.model_validate(data)

    def test_confidence_below_0_raises(self):
        data = {
            "severity": "LOW",
            "category": "UNKNOWN",
            "is_actionable": False,
            "summary": "x",
            "root_cause_hypothesis": "y",
            "confidence": -0.1,  # invalid
        }
        with pytest.raises(ValidationError):
            TriageResult.model_validate(data)

    def test_empty_evidence_list_allowed(self):
        data = {
            "severity": "LOW",
            "category": "UNKNOWN",
            "is_actionable": False,
            "summary": "noise",
            "root_cause_hypothesis": "routine",
            "confidence": 0.99,
        }
        result = TriageResult.model_validate(data)
        assert result.evidence == []

    def test_all_severity_values_valid(self):
        for sev in ("LOW", "MEDIUM", "HIGH", "CRITICAL"):
            data = {
                "severity": sev,
                "category": "UNKNOWN",
                "is_actionable": False,
                "summary": "x",
                "root_cause_hypothesis": "y",
                "confidence": 0.5,
            }
            result = TriageResult.model_validate(data)
            assert result.severity == sev

    def test_all_category_values_valid(self):
        for cat in (
            "APPLICATION_ERROR",
            "DATABASE_ERROR",
            "DEPENDENCY_ERROR",
            "CONFIGURATION_ERROR",
            "INFRASTRUCTURE_ERROR",
            "NETWORK_ERROR",
            "AUTHENTICATION_ERROR",
            "UNKNOWN",
        ):
            data = {
                "severity": "LOW",
                "category": cat,
                "is_actionable": False,
                "summary": "x",
                "root_cause_hypothesis": "y",
                "confidence": 0.5,
            }
            result = TriageResult.model_validate(data)
            assert result.category == cat
