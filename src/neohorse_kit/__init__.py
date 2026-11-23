"""neohorse-kit: community toolkit for the NeoHorse JEV decision API — key-pool
client, typed questions/answers, presets, CLI and MCP server."""

from .client import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    AsyncNeoHorseClient,
    NeoHorseAuthError,
    NeoHorseBadRequest,
    NeoHorseClient,
    NeoHorseError,
    check_key_format,
    normalize_questions,
)
from . import presets as presets
from . import types as types
from .types import (
    Choice,
    ChoiceAnswer,
    ClassifyResult,
    ModelInfo,
    ModelsResult,
    Noul,
    NoulAnswer,
    Score,
    ScoreAnswer,
    Usage,
)

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "AsyncNeoHorseClient",
    "Choice",
    "ChoiceAnswer",
    "ClassifyResult",
    "ModelInfo",
    "ModelsResult",
    "NeoHorseAuthError",
    "NeoHorseBadRequest",
    "NeoHorseClient",
    "NeoHorseError",
    "Noul",
    "NoulAnswer",
    "Score",
    "ScoreAnswer",
    "Usage",
    "check_key_format",
    "normalize_questions",
    "presets",
    "types",
    "__version__",
]
