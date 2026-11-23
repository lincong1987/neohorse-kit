"""Typed (pydantic) questions and answers for the NeoHorse JEV decision API.

Questions are built by :mod:`neohorse_kit.presets` or directly::

    from neohorse_kit.types import Noul, Choice, Score

    Noul(instructions="Should we stop?", criteria=NoulCriteria(true="light is red"))
    Choice(instructions="Which lane?", criteria={"left": "turn", "straight": None})
    Score(instructions="Risk level?", criteria=["low", "medium", "high"])

:class:`ClassifyResult` is what :meth:`NeoHorseClient.decide` returns —
attribute access throughout; ``.model_dump()`` for a plain dict.

Wire notes (verified against the live API and the platform docs):

- ``state`` is the background or content under judgement — a string is
  recommended, any JSON value is accepted.
- ``noul``   -> yes/no question; the answer's ``noul`` is the probability of
  yes (0..1). ``/v1/decision`` additionally returns per-outcome
  ``probabilities``.
- ``choice`` -> one label from an unordered set (1..255 options), ``criteria``
  maps ``{label: description}``; returns ``choice`` + ``probabilities`` +
  ``confidence``.
- ``score``  -> an expected level on an ordered scale (2..10 levels),
  ``criteria`` lists level labels from low to high; returns ``score`` (the
  expected index) + ``probabilities`` + ``confidence``. ``legend`` may come
  back as a list or as a ``{"0": "low", ...}`` mapping — use ``levels``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _KitModel(BaseModel):
    # "model" is an API response field; opt out of pydantic's model_ namespace.
    model_config = ConfigDict(protected_namespaces=())


def _wire_excluding(d: dict, keys: tuple[str, ...]) -> dict:
    return {k: v for k, v in d.items() if k not in keys and v is not None}


class _Question(_KitModel):
    """Base for typed questions; ``name`` keys the answer and never hits the wire."""

    name: str | None = None

    def wire(self) -> dict:
        """The upstream JSON for this question, without its ``name``."""
        raise NotImplementedError


class NoulCriteria(_KitModel):
    """Optional descriptions of the yes and no outcomes."""

    true: str | None = None
    false: str | None = None


class Noul(_Question):
    """Yes/no question; the answer's ``noul`` is the probability of yes."""

    type: Literal["noul"] = "noul"
    instructions: str | None = None
    criteria: NoulCriteria | None = None

    def wire(self) -> dict:
        d = _wire_excluding(self.model_dump(mode="json"), ("name",))
        if self.criteria is not None:
            # keep explicit nulls: the API schema allows them and they mean "undescribed"
            d["criteria"] = self.criteria.model_dump(mode="json")
        return d


class Choice(_Question):
    """One label from an unordered set; ``criteria`` maps label -> description."""

    type: Literal["choice"] = "choice"
    instructions: str | None = None
    criteria: dict[str, str | None]

    def wire(self) -> dict:
        d = _wire_excluding(self.model_dump(mode="json"), ("name",))
        d["criteria"] = dict(self.criteria)  # preserve explicit nulls
        return d


class Score(_Question):
    """A place on an ordered scale; ``criteria`` lists the level labels."""

    type: Literal["score"] = "score"
    instructions: str | None = None
    criteria: list[str]

    def wire(self) -> dict:
        return _wire_excluding(self.model_dump(mode="json"), ("name",))


Question = Annotated[Union[Noul, Choice, Score], Field(discriminator="type")]


class NoulAnswer(_KitModel):
    type: Literal["noul"] = "noul"
    noul: float
    """Probability of yes, 0..1."""
    probabilities: dict[str, float] | None = None
    """Per-outcome probabilities; present on ``/v1/decision``."""


class ChoiceAnswer(_KitModel):
    type: Literal["choice"] = "choice"
    choice: str
    confidence: float | None = None
    probabilities: dict[str, float] = Field(default_factory=dict)


class ScoreAnswer(_KitModel):
    type: Literal["score"] = "score"
    score: float
    """Expected level in the index space of the question's ``criteria``."""
    confidence: float | None = None
    legend: dict[str, str] | list[str] | None = None
    """The level labels, as a list or a ``{"0": "low"}`` mapping."""
    probabilities: dict[str, float] = Field(default_factory=dict)
    """Level indexes as string keys, e.g. ``{"0": 0.29, "1": 0.30}``."""

    @property
    def levels(self) -> list[str]:
        """The ordered level labels, whatever shape upstream sent."""
        if isinstance(self.legend, list):
            return self.legend
        if isinstance(self.legend, dict):
            return [self.legend[k] for k in sorted(self.legend, key=int)]
        return []


Answer = Annotated[Union[NoulAnswer, ChoiceAnswer, ScoreAnswer], Field(discriminator="type")]


class Usage(_KitModel):
    """Token accounting; which fields are filled depends on the endpoint.

    ``/v1/systemone`` reports ``usage.input_tokens`` / ``usage.output_tokens``
    (the latter is the serialized-answer length, not generated tokens).
    ``/v1/decision`` reports top-level ``input_tokens`` (falling back to
    ``input_details.expanded_tokens``) and no output tokens.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None


class ModelInfo(_KitModel):
    name: str
    owned_by: str | None = None
    context_length: int | None = None

    @model_validator(mode="before")
    @classmethod
    def _accept_id(cls, data: Any) -> Any:
        # /v1/models speaks the OpenAI dialect: {"id": "..."}.
        if isinstance(data, dict) and "id" in data and "name" not in data:
            data = {**data, "name": data["id"]}
        return data


class ModelsResult(_KitModel):
    models: list[ModelInfo] = Field(default_factory=list)
    latency_ms: float | None = None
    """Added by neohorse-kit: round trip as seen from the client."""

    @model_validator(mode="before")
    @classmethod
    def _accept_data_list(cls, data: Any) -> Any:
        # /v1/models wraps entries in {"object": "list", "data": [...]}.
        if isinstance(data, dict) and "data" in data and "models" not in data:
            data = {**data, "models": data["data"]}
        return data


class ClassifyResult(_KitModel):
    """Response of :meth:`neohorse_kit.NeoHorseClient.decide`."""

    model: str | None = None
    answers: dict[str, Answer] = Field(default_factory=dict)
    usage: Usage = Field(default_factory=Usage)
    input_tokens: int | None = None
    """``/v1/decision`` top-level packed input tokens."""
    input_details: dict[str, Any] | None = None
    """``/v1/decision`` details: expanded_tokens, question_count, image info."""
    extensions: dict[str, Any] | None = None
    """``/v1/systemone`` backend extras (probability source, definitions...)."""
    decision_seconds: float | None = None
    """Upstream decision time in seconds."""
    request_id: str | None = None
    latency_ms: float | None = None
    """Added by neohorse-kit: round trip as seen from the client."""

    @model_validator(mode="before")
    @classmethod
    def _fill_usage(cls, data: Any) -> Any:
        """Normalize the two endpoints' token conventions into ``usage``.

        ``/v1/decision`` reports top-level ``input_tokens`` (falling back to
        ``input_details.expanded_tokens``); ``/v1/systemone`` reports a
        ``usage`` block directly. The two are never summed.
        """
        if not isinstance(data, dict) or data.get("usage"):
            return data
        input_tokens = data.get("input_tokens")
        if input_tokens is None:
            details = data.get("input_details")
            if isinstance(details, dict):
                input_tokens = details.get("expanded_tokens")
        if input_tokens is not None:
            data = {**data, "usage": {"input_tokens": input_tokens}}
        return data
