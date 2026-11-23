"""Tests for the typed question/answer layer."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from neohorse_kit.types import (
    Choice,
    ChoiceAnswer,
    ClassifyResult,
    Noul,
    NoulCriteria,
    Score,
    ScoreAnswer,
)
from neohorse_kit.client import normalize_questions
from neohorse_kit import presets


class TestQuestionWire:
    def test_noul_wire_strips_name_keeps_criteria_nulls(self):
        q = Noul(name="should_stop", instructions="现在是否应停车？",
                 criteria=NoulCriteria(true="红灯且有行人", false=None))
        assert q.wire() == {
            "type": "noul",
            "instructions": "现在是否应停车？",
            "criteria": {"true": "红灯且有行人", "false": None},
        }

    def test_noul_without_criteria(self):
        assert presets.urgency().wire() == {
            "type": "noul", "instructions": "Does this convey urgency?",
        }

    def test_choice_wire_preserves_label_nulls(self):
        q = Choice(name="lane", instructions="Which lane?",
                   criteria={"left": "turn", "straight": None})
        assert q.wire()["criteria"] == {"left": "turn", "straight": None}

    def test_score_wire(self):
        q = Score(name="risk", instructions="危险程度？", criteria=["低", "中", "高"])
        assert q.wire() == {
            "type": "score", "instructions": "危险程度？", "criteria": ["低", "中", "高"],
        }

    def test_discriminated_union_parses_answers(self):
        result = ClassifyResult.model_validate({
            "model": "NeoHorse-Jev-4B",
            "answers": {
                "u": {"type": "noul", "noul": 0.7161,
                      "probabilities": {"false": 0.2839, "true": 0.7161}},
                "c": {"type": "choice", "choice": "billing", "confidence": 0.85,
                      "probabilities": {"billing": 0.9, "technical": 0.05}},
                "s": {"type": "score", "score": 1.31, "confidence": 0.7,
                      "legend": {"0": "minor", "1": "moderate"},
                      "probabilities": {"0": 0.2, "1": 0.8}},
            },
            "usage": {"input_tokens": 26, "output_tokens": 24},
            "decision_seconds": 0.052,
            "latency_ms": 150.0,
        })
        assert result.answers["u"].noul == 0.7161
        assert result.answers["u"].probabilities["true"] == 0.7161
        assert result.answers["c"].choice == "billing"
        assert result.answers["s"].probabilities["1"] == 0.8
        assert result.answers["s"].levels == ["minor", "moderate"]
        # model field must not trip pydantic's model_ namespace
        assert result.model == "NeoHorse-Jev-4B"
        dumped = result.model_dump(mode="json")
        assert dumped["answers"]["c"]["choice"] == "billing"

    def test_score_legend_accepts_list_too(self):
        ans = ScoreAnswer.model_validate({"type": "score", "score": 0.0,
                                          "legend": ["low", "high"]})
        assert ans.levels == ["low", "high"]

    def test_bad_answer_type_rejected(self):
        with pytest.raises(ValidationError):
            ClassifyResult.model_validate({
                "model": "NeoHorse-Jev-4B",
                "answers": {"x": {"type": "choice", "choice": 3}},
            })


class TestModelsResultParsing:
    def test_openai_data_shape_mapped_to_models(self):
        from neohorse_kit.types import ModelsResult

        result = ModelsResult.model_validate({
            "object": "list",
            "data": [{"id": "neohorse-1.5-flash-preview", "owned_by": "tokenrhythm",
                      "context_length": 1048576}],
        })
        assert result.models[0].name == "neohorse-1.5-flash-preview"
        assert result.models[0].context_length == 1048576


class TestNormalizeAcceptsTyped:
    def test_single_model(self):
        assert normalize_questions(presets.urgency()) == {
            "is_urgent": {"type": "noul", "instructions": "Does this convey urgency?"},
        }

    def test_mixed_list(self):
        out = normalize_questions([presets.urgency(), {"name": "raw", "type": "noul", "instructions": "r"}])
        assert set(out) == {"is_urgent", "raw"}
        assert out["is_urgent"]["instructions"] == "Does this convey urgency?"
        assert out["raw"]["instructions"] == "r"

    def test_mapping_of_models(self):
        out = normalize_questions({"a": presets.urgency()})
        assert out["a"]["type"] == "noul"

    def test_dict_form_still_works(self):
        out = normalize_questions({"a": {"type": "noul", "instructions": "x"}})
        assert out == {"a": {"type": "noul", "instructions": "x"}}
