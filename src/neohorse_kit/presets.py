"""Question builders for the NeoHorse JEV decision API.

Each builder returns a typed question (:mod:`neohorse_kit.types`) that
:class:`neohorse_kit.NeoHorseClient.decide` sends as-is. All three upstream
question types (verified against the live API and the platform docs):

- ``noul``   -> binary question, ``{"noul": 0.0..1.0}`` (probability of yes);
  optional ``criteria`` descriptions sharpen what yes/no mean.
- ``choice`` -> one label from an unordered set (1..255 options),
  ``criteria`` maps ``{label: description}``; returns ``choice`` + per-label
  probabilities + confidence.
- ``score``  -> position on an ordered scale (2..10 levels), ``criteria`` is
  the list of level labels from low to high; returns continuous ``score`` +
  per-level probabilities + confidence.

A text-only call takes 1..16 batched questions; ``state`` may be any JSON
value, though a string is recommended.
"""

from __future__ import annotations

from .types import Choice, Noul, NoulCriteria, Score


def noul(
    question: str,
    *,
    name: str = "answer",
    true: str | None = None,
    false: str | None = None,
) -> Noul:
    """Binary question scored 0..1 (e.g. "Does this convey urgency?").

    ``true``/``false`` optionally describe what each outcome means, which
    sharpens the score.
    """
    criteria = NoulCriteria(true=true, false=false) if (true or false) else None
    return Noul(name=name, instructions=question, criteria=criteria)


def choice(
    question: str,
    options: dict[str, str | None],
    *,
    name: str = "answer",
) -> Choice:
    """Single-label classification over an unordered set of ``options``.

    ``options`` maps each label to a short description (or ``None``), e.g.
    ``{"billing": "payments, payouts, invoices", "technical": "bugs, outages"}``.
    """
    return Choice(name=name, instructions=question, criteria=dict(options))


def score(question: str, levels: list[str], *, name: str = "answer") -> Score:
    """Rated question over ordered ``levels`` (e.g. calm/annoyed/angry/furious)."""
    return Score(name=name, instructions=question, criteria=list(levels))


def urgency(*, name: str = "is_urgent") -> Noul:
    """Preset: does the text convey urgency?"""
    return noul("Does this convey urgency?", name=name)


def sentiment(*, name: str = "sentiment") -> Score:
    """Preset: overall sentiment as negative / neutral / positive."""
    return score(
        "What is the overall sentiment of this text?",
        ["negative", "neutral", "positive"],
        name=name,
    )


def severity(*, name: str = "severity") -> Score:
    """Preset: issue severity from minor to critical."""
    return score(
        "How severe is the issue described?",
        ["minor", "moderate", "major", "critical"],
        name=name,
    )


def priority(*, name: str = "priority") -> Choice:
    """Preset: triage priority as a choice over low/medium/high/urgent."""
    return choice(
        "What priority should this get?",
        {
            "low": "no rush, handle whenever",
            "medium": "handle soon, not blocking",
            "high": "should be handled today",
            "urgent": "drop everything, someone is blocked or losing money",
        },
        name=name,
    )
