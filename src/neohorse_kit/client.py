"""Client for the NeoHorse JEV decision API (Token Rhythm).

JEV is a structured *decision* model — not a chat model. You send a ``state``
(background or content under judgement) plus 1..16 questions and get
probability-scored answers back in one round trip. The scores are an explicit
distribution over outcomes, but — per the platform docs — *not* calibrated
correctness rates: pick your own thresholds. Two endpoints accept the same
request body:

- ``POST /v1/decision`` (recommended): answers plus top-level ``input_tokens``
  / ``input_details`` / ``decision_seconds``.
- ``POST /v1/systemone``: System One compatible shape — answers plus
  ``usage`` / ``extensions``.

What this adds over raw HTTP calls:

- **API-key pool**: several keys with automatic rotation — 401/403 (dead key or
  missing grayscale access) and 429 (capacity limit) move to the next key at
  once.
- **In-key retries with backoff**: transient failures (408/5xx, network and
  timeout errors) are retried on the same key with exponential backoff and
  jitter, honouring ``Retry-After`` headers, inside a total time budget.
  With a single configured key, 429 is also retried this way.
- **LRU response cache**: upstream answers are deterministic for identical
  input, so repeats are served for free.
- **Per-key usage accounting**: calls / input / output tokens per key, split
  by the endpoint's own token convention (see :class:`~.types.Usage`).
- **Key format validation** at construction time (``sk_`` + base64url).
- **Protocol guards**: the documented limits (1..16 questions, choice 1..255
  options, score 2..10 levels, image = one question, body size caps) are
  checked client-side with actionable errors instead of a bare upstream 422.
- **Image questions**: a PNG goes in the top-level ``image`` field as a
  base64 data URL; pass bytes or a path and it is converted for you.

Keys come from the ``keys`` argument, or the ``NEOHORSE_API_KEYS`` environment
variable (comma-separated); ``NEOHORSE_API_KEY`` is also accepted.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import itertools
import json
import os
import random
import re
import time
from collections import OrderedDict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx

from .types import Choice, ClassifyResult, ModelInfo, ModelsResult, Noul, Score

DEFAULT_BASE_URL = "https://tokenrhythm.studio/v1"
DEFAULT_MODEL = "NeoHorse-Jev-4B"
VALID_TYPES = ("noul", "choice", "score")
ENDPOINTS = ("decision", "systemone")

#: Keys with these statuses are dead for this request; try the next key.
#: 403 here means the key lacks JEV grayscale access.
ROTATE_STATUSES = frozenset({401, 403})
#: Transient statuses retried on the same key (429 rotates unless it is the only key).
RETRY_STATUSES = frozenset({408, *range(500, 600)})

#: Token Rhythm platform keys look like sk_ + base64url (seen: sk_tr_ + 43 chars).
API_KEY_PATTERN = re.compile(r"^sk_[A-Za-z0-9_-]{16,}$")

#: Documented request limits (the upstream answers 422 when they are exceeded).
MAX_QUESTIONS = 16
MAX_CHOICE_OPTIONS = 255
MIN_SCORE_LEVELS = 2
MAX_SCORE_LEVELS = 10
MAX_BODY_BYTES = 1024 * 1024  # 1 MiB without an image
MAX_IMAGE_BODY_BYTES = 8 * 1024 * 1024  # 8 MiB with an image

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_IMAGE_DATA_URL = "data:image/png;base64,"


class NeoHorseError(RuntimeError):
    """Base error; message carries the upstream detail."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        trace_id: str | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.trace_id = trace_id
        self.status = status


class NeoHorseAuthError(NeoHorseError):
    """All keys failed auth/grayscale, or no usable key was configured."""


class NeoHorseBadRequest(NeoHorseError):
    """Upstream rejected the request payload, or a documented limit was hit
    locally (non-retryable)."""


def check_key_format(key: str) -> str:
    """Return the key when it matches the Token Rhythm shape, else raise.

    A malformed key usually means a truncated copy or a key from another
    product; failing at construction beats failing (and confusing) on the
    first request.
    """
    k = key.strip()
    if not API_KEY_PATTERN.match(k):
        raise NeoHorseAuthError(
            f"not a Token Rhythm API key: expected 'sk_' + at least 16 "
            f"characters, got {key[:18]}… ({len(key)} chars)"
        )
    return k


def default_keys() -> list[str]:
    raw = os.environ.get("NEOHORSE_API_KEYS") or os.environ.get("NEOHORSE_API_KEY") or ""
    return [k.strip() for k in raw.split(",") if k.strip()]


def normalize_questions(questions: Any) -> dict[str, dict[str, Any]]:
    """Accept typed questions (:class:`~.types.Noul` / `Choice` / `Score`),
    a ``{name: question}`` mapping, a single question, or a list — mixed forms
    welcome. Returns the wire format ``{name: question-json}`` (name defaults
    to ``"answer"``).
    """
    def split(item: Any) -> tuple[str, dict[str, Any]]:
        if isinstance(item, (Noul, Choice, Score)):
            return (item.name or "answer"), item.wire()
        q = dict(item)
        if "type" not in q:
            raise NeoHorseBadRequest(f"question missing 'type': {q!r}")
        return (q.pop("name", None) or "answer"), q

    if isinstance(questions, (Noul, Choice, Score)):
        name, wire = split(questions)
        return {name: wire}
    if isinstance(questions, Mapping):
        if "type" in questions or isinstance(questions, (Noul, Choice, Score)):
            name, wire = split(questions)
            return {name: wire}
        out: dict[str, dict[str, Any]] = {}
        for name, item in questions.items():
            wire = item.wire() if isinstance(item, (Noul, Choice, Score)) else dict(item)
            out[str(name)] = wire
        return out
    if isinstance(questions, (list, tuple)):
        out = {}
        for item in questions:
            name, wire = split(item)
            base = name
            i = 2
            while name in out:
                name = f"{base}_{i}"
                i += 1
            out[name] = wire
        return out
    raise NeoHorseBadRequest(f"unsupported questions format: {type(questions).__name__}")


def check_limits(questions: Mapping[str, Any], *, has_image: bool = False) -> None:
    """Enforce the documented protocol limits before spending a request.

    Text-only calls take 1..16 questions; a call carrying an image takes
    exactly one. ``choice`` allows 1..255 options, ``score`` 2..10 levels.
    Violations raise :class:`NeoHorseBadRequest` with the upstream's own
    wording so the fix is obvious.
    """
    count = len(questions)
    if has_image and count != 1:
        raise NeoHorseBadRequest(
            f"image questions must be sent alone: got {count} questions, "
            "the API allows exactly 1 when 'image' is present"
        )
    if not 1 <= count <= MAX_QUESTIONS:
        raise NeoHorseBadRequest(
            f"expected 1..{MAX_QUESTIONS} questions per call, got {count}"
        )
    for name, q in questions.items():
        qtype = q.get("type")
        if qtype == "choice":
            options = q.get("criteria") or {}
            if not 1 <= len(options) <= MAX_CHOICE_OPTIONS:
                raise NeoHorseBadRequest(
                    f"choice question '{name}' needs 1..{MAX_CHOICE_OPTIONS} "
                    f"options, got {len(options)}"
                )
        elif qtype == "score":
            levels = q.get("criteria") or []
            if not MIN_SCORE_LEVELS <= len(levels) <= MAX_SCORE_LEVELS:
                raise NeoHorseBadRequest(
                    f"score question '{name}' needs {MIN_SCORE_LEVELS}.."
                    f"{MAX_SCORE_LEVELS} levels, got {len(levels)}"
                )


def image_data_url(value: str | bytes | Path) -> str:
    """Return a PNG base64 data URL for ``value``.

    Accepts an existing ``data:image/png;base64,...`` URL (returned as-is),
    raw PNG bytes, or a path to a ``.png`` file. JEV takes PNG only.
    """
    if isinstance(value, str):
        if value.startswith(_IMAGE_DATA_URL):
            return value
        path = Path(value)
        if path.is_file():
            return _png_bytes_to_data_url(path.read_bytes())
        raise NeoHorseBadRequest(
            "image must be a PNG data URL or a path to a .png file, got a "
            f"string that is neither: {value[:40]}…"
        )
    if isinstance(value, Path):
        return _png_bytes_to_data_url(value.read_bytes())
    if isinstance(value, (bytes, bytearray)):
        return _png_bytes_to_data_url(bytes(value))
    raise NeoHorseBadRequest(f"unsupported image type: {type(value).__name__}")


def _png_bytes_to_data_url(data: bytes) -> str:
    if not data.startswith(_PNG_MAGIC):
        raise NeoHorseBadRequest("image must be a PNG (the JEV API accepts PNG only)")
    return _IMAGE_DATA_URL + base64.b64encode(data).decode("ascii")


def plan_for_status(status: int, *, single_key: bool) -> str:
    """What to do with a non-200: ``rotate`` to the next key, ``retry`` this
    one, or ``raise`` (payload errors nothing will fix)."""
    if status in ROTATE_STATUSES:
        return "rotate"
    if status == 429:
        # Several keys: another key likely has capacity. One key: wait it out.
        return "retry" if single_key else "rotate"
    if status in RETRY_STATUSES:
        return "retry"
    if 400 <= status < 500:
        # 400/413/422 and friends: the payload itself is the problem.
        return "raise"
    return "retry"


def error_for_status(resp: httpx.Response) -> NeoHorseError:
    """Build an error carrying the platform's ``code`` / ``message`` /
    ``traceId`` (top level, not wrapped in an ``error`` object)."""
    code = message = trace_id = None
    try:
        body = resp.json()
        if isinstance(body, dict):
            code = body.get("code")
            message = body.get("message")
            trace_id = body.get("traceId")
            err = body.get("error")
            if isinstance(err, dict):  # tolerate the nested dialect too
                message = message or err.get("message")
                trace_id = trace_id or err.get("traceId") or err.get("trace_id")
    except Exception:
        pass
    text = f"HTTP {resp.status_code}: {message or resp.text[:300]}"
    if trace_id:
        text += f" (traceId {trace_id})"
    if resp.status_code in ROTATE_STATUSES:
        return NeoHorseAuthError(
            text, code=code, trace_id=trace_id, status=resp.status_code
        )
    if 400 <= resp.status_code < 500:
        return NeoHorseBadRequest(
            text, code=code, trace_id=trace_id, status=resp.status_code
        )
    return NeoHorseError(text, code=code, trace_id=trace_id, status=resp.status_code)


def retry_after_seconds(resp: httpx.Response) -> float | None:
    """Seconds the server asked us to wait, from ``Retry-After`` (seconds) or
    ``retry-after-ms`` (milliseconds); ``None`` when absent or unparsable."""
    for header, scale in (("retry-after-ms", 0.001), ("Retry-After", 1.0)):
        value = resp.headers.get(header)
        if value is None:
            continue
        try:
            seconds = float(value) * scale
        except ValueError:
            continue
        if seconds >= 0:
            return seconds
    return None


def backoff_delay(attempt: int, initial: float, maximum: float, jitter: float) -> float:
    """Exponential backoff with jitter: ``initial * 2**(attempt-1)``, capped at
    ``maximum``, a random ``jitter`` fraction subtracted. Zero ``initial``
    disables waiting (tests)."""
    if initial <= 0:
        return 0.0
    exponential = min(initial * (2 ** (attempt - 1)), maximum)
    return max(0.0, exponential * (1.0 - random.random() * jitter))


def cache_key_for(payload: Mapping[str, Any]) -> str:
    """Stable cache key for a request payload (sha256 of its canonical JSON).

    Hashing keeps the key small even when ``image`` carries a large data URL.
    """
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def record_usage(result: Mapping[str, Any]) -> tuple[int, int]:
    """Extract ``(input_tokens, output_tokens)`` from a raw response.

    ``/v1/decision`` reports top-level ``input_tokens`` (fall back to
    ``input_details.expanded_tokens``) and no output tokens;
    ``/v1/systemone`` reports ``usage.input_tokens`` / ``usage.output_tokens``.
    The two conventions are never summed.
    """
    usage = result.get("usage")
    if isinstance(usage, Mapping):
        return (
            int(usage.get("input_tokens") or 0),
            int(usage.get("output_tokens") or 0),
        )
    input_tokens = result.get("input_tokens")
    if input_tokens is None:
        details = result.get("input_details")
        if isinstance(details, Mapping):
            input_tokens = details.get("expanded_tokens")
    return (int(input_tokens or 0), 0)


class _Base:
    def __init__(
        self,
        keys: list[str] | str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        endpoint: str = "decision",
        timeout: float = 30.0,
        cache_size: int = 256,
        transport: Any = None,
        check_keys: bool = True,
        max_retries: int = 2,
        backoff_initial: float = 0.5,
        backoff_max: float = 5.0,
        backoff_jitter: float = 0.25,
        retry_budget: float = 30.0,
    ) -> None:
        if endpoint not in ENDPOINTS:
            raise NeoHorseBadRequest(
                f"endpoint must be one of {ENDPOINTS}, got {endpoint!r}"
            )
        if isinstance(keys, str):
            keys = [k.strip() for k in keys.split(",") if k.strip()]
        self._keys = list(keys) if keys else default_keys()
        if not self._keys:
            raise NeoHorseAuthError("no API keys: pass keys= or set NEOHORSE_API_KEYS")
        if check_keys:
            self._keys = [check_key_format(k) for k in self._keys]
        self._cycle = itertools.cycle(range(len(self._keys)))
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.endpoint = endpoint
        self.timeout = timeout
        self.cache_size = cache_size
        self.max_retries = max_retries
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max
        self.backoff_jitter = backoff_jitter
        self.retry_budget = retry_budget
        self._cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._transport = transport
        self.usage = {
            self.hint(k): {"calls": 0, "input_tokens": 0, "output_tokens": 0}
            for k in self._keys
        }

    def hint(self, key: str) -> str:
        return key[:12] + "…"

    def _next_key(self) -> tuple[int, str]:
        idx = next(self._cycle)
        return idx, self._keys[idx]

    def _single_key(self) -> bool:
        return len(set(self._keys)) == 1

    def _payload(
        self, state: Any, questions: Any, image: str | bytes | Path | None
    ) -> dict[str, Any]:
        wire = normalize_questions(questions)
        image_url = image_data_url(image) if image is not None else None
        check_limits(wire, has_image=image_url is not None)
        payload: dict[str, Any] = {
            "model": self.model,
            "state": state,
            "questions": wire,
        }
        if image_url is not None:
            payload["image"] = image_url
        cap = MAX_IMAGE_BODY_BYTES if image_url is not None else MAX_BODY_BYTES
        size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        if size > cap:
            raise NeoHorseBadRequest(
                f"request body is {size / 1024 / 1024:.2f} MiB, over the "
                f"{cap // 1024 // 1024} MiB limit"
                + (" for image requests" if image_url is not None else "")
            )
        return payload

    def _cache_get(self, key: str) -> dict[str, Any] | None:
        if not self.cache_size:
            return None
        hit = self._cache.get(key)
        if hit is not None:
            self._cache.move_to_end(key)
            return copy.deepcopy(hit)
        return None

    def _cache_put(self, key: str, value: dict[str, Any]) -> None:
        if not self.cache_size:
            return
        self._cache[key] = copy.deepcopy(value)
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)

    def _record(self, key_idx: int, result: dict[str, Any]) -> None:
        stats = self.usage[self.hint(self._keys[key_idx])]
        stats["calls"] += 1
        input_tokens, output_tokens = record_usage(result)
        stats["input_tokens"] += input_tokens
        stats["output_tokens"] += output_tokens

    def _next_delay(self, attempt: int, resp: httpx.Response | None) -> float:
        """Seconds to wait before retrying this key: the server's word when it
        gave one, else backoff. Capped at ``backoff_max``."""
        if resp is not None:
            asked = retry_after_seconds(resp)
            if asked is not None:
                return min(asked, self.backoff_max)
        return backoff_delay(attempt, self.backoff_initial, self.backoff_max, self.backoff_jitter)


class NeoHorseClient(_Base):
    """Synchronous client."""

    def decide(
        self,
        state: Any,
        questions: Any,
        *,
        image: str | bytes | Path | None = None,
        endpoint: str | None = None,
    ) -> ClassifyResult:
        """Judge ``state`` (any JSON value — text is recommended) with 1..16
        batched questions; returns a typed
        :class:`~.types.ClassifyResult` (``.model_dump()`` for a dict).

        ``image`` attaches one PNG question (data URL, bytes or path); with an
        image exactly one question is allowed. ``endpoint`` overrides the
        client's default (``"decision"`` or the System One compatible
        ``"systemone"``).
        """
        payload = self._payload(state, questions, image)
        key = cache_key_for(payload)
        hit = self._cache_get(key)
        if hit is not None:
            return ClassifyResult.model_validate(hit)
        path = f"/{endpoint or self.endpoint}"
        return ClassifyResult.model_validate(self._send("POST", path, payload, key))

    # Familiar alias: the API is a decision API, classify() reads naturally too.
    classify = decide

    def models(self) -> ModelsResult:
        """List the models the account can call (works at any balance; handy
        as a key health check). JEV itself is not a chat model, so it does not
        appear in this catalog."""
        return ModelsResult.model_validate(self._send("GET", "/models", None, None))

    def _send(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        cache_key: str | None,
    ) -> dict[str, Any]:
        content = json.dumps(body) if body is not None else None
        single = self._single_key()
        last_error: Exception | None = None
        started = time.monotonic()
        with httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout,
            headers={"Content-Type": "application/json"},
            transport=self._transport,
        ) as http:
            for _ in range(len(self._keys)):
                idx, key = self._next_key()
                attempt = 0
                while True:
                    attempt += 1
                    start = time.perf_counter()
                    resp: httpx.Response | None = None
                    try:
                        resp = http.request(
                            method,
                            path,
                            content=content,
                            headers={"Authorization": f"Bearer {key}"},
                        )
                    except httpx.HTTPError as exc:
                        last_error = NeoHorseError(f"network error: {exc}")
                        plan = "retry"
                    if resp is not None:
                        latency_ms = round((time.perf_counter() - start) * 1000, 1)
                        if resp.status_code == 200:
                            result = resp.json()
                            result["latency_ms"] = latency_ms
                            self._record(idx, result)
                            if cache_key is not None:
                                self._cache_put(cache_key, result)
                            return result
                        plan = plan_for_status(resp.status_code, single_key=single)
                        last_error = error_for_status(resp)
                        if plan == "raise":
                            raise last_error
                    if plan == "rotate":
                        break
                    # retry this key, unless the retry budget says stop waiting
                    if attempt > self.max_retries:
                        break
                    delay = self._next_delay(attempt, resp)
                    if time.monotonic() - started + delay >= self.retry_budget:
                        break
                    time.sleep(delay)
        raise last_error or NeoHorseAuthError("all keys exhausted")


class AsyncNeoHorseClient(_Base):
    """Async client; same behaviour as :class:`NeoHorseClient`."""

    async def decide(
        self,
        state: Any,
        questions: Any,
        *,
        image: str | bytes | Path | None = None,
        endpoint: str | None = None,
    ) -> ClassifyResult:
        payload = self._payload(state, questions, image)
        key = cache_key_for(payload)
        hit = self._cache_get(key)
        if hit is not None:
            return ClassifyResult.model_validate(hit)
        path = f"/{endpoint or self.endpoint}"
        return ClassifyResult.model_validate(await self._send("POST", path, payload, key))

    classify = decide

    async def models(self) -> ModelsResult:
        return ModelsResult.model_validate(await self._send("GET", "/models", None, None))

    async def _send(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        cache_key: str | None,
    ) -> dict[str, Any]:
        content = json.dumps(body) if body is not None else None
        single = self._single_key()
        last_error: Exception | None = None
        started = time.monotonic()
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
            headers={"Content-Type": "application/json"},
            transport=self._transport,
        ) as http:
            for _ in range(len(self._keys)):
                idx, key = self._next_key()
                attempt = 0
                while True:
                    attempt += 1
                    start = time.perf_counter()
                    resp: httpx.Response | None = None
                    try:
                        resp = await http.request(
                            method,
                            path,
                            content=content,
                            headers={"Authorization": f"Bearer {key}"},
                        )
                    except httpx.HTTPError as exc:
                        last_error = NeoHorseError(f"network error: {exc}")
                        plan = "retry"
                    if resp is not None:
                        latency_ms = round((time.perf_counter() - start) * 1000, 1)
                        if resp.status_code == 200:
                            result = resp.json()
                            result["latency_ms"] = latency_ms
                            self._record(idx, result)
                            if cache_key is not None:
                                self._cache_put(cache_key, result)
                            return result
                        plan = plan_for_status(resp.status_code, single_key=single)
                        last_error = error_for_status(resp)
                        if plan == "raise":
                            raise last_error
                    if plan == "rotate":
                        break
                    if attempt > self.max_retries:
                        break
                    delay = self._next_delay(attempt, resp)
                    if time.monotonic() - started + delay >= self.retry_budget:
                        break
                    await asyncio.sleep(delay)
        raise last_error or NeoHorseAuthError("all keys exhausted")
