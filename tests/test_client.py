"""Unit tests with a mocked httpx transport (no network access)."""

from __future__ import annotations

import base64
import json
import struct
import zlib

import httpx
import pytest

from neohorse_kit.client import (
    AsyncNeoHorseClient,
    NeoHorseAuthError,
    NeoHorseBadRequest,
    NeoHorseClient,
    check_key_format,
    normalize_questions,
)
from neohorse_kit import presets
from neohorse_kit.types import Choice, Noul, NoulCriteria, Score

# /v1/decision shape: noul answers carry probabilities; tokens on top level.
DECISION_OK = {
    "model": "NeoHorse-Jev-4B",
    "answers": {
        "is_urgent": {
            "type": "noul",
            "noul": 0.7161,
            "probabilities": {"false": 0.2839, "true": 0.7161},
        },
    },
    "input_tokens": 26,
    "probabilities": {"is_urgent": {"false": 0.2839, "true": 0.7161}},
    "input_details": {"input_tokens": 26, "expanded_tokens": 26, "question_count": 1},
    "batch_stats": {},
    "probability_source": "pointer_head",
    "decision_seconds": 0.052,
}

# /v1/systemone shape: no noul probabilities; usage + extensions blocks.
SYSTEMONE_OK = {
    "model": "NeoHorse-Jev-4B",
    "answers": {
        "is_urgent": {"type": "noul", "noul": 0.7161},
        "priority": {
            "type": "choice",
            "choice": "urgent",
            "confidence": 0.85,
            "probabilities": {"low": 0.02, "urgent": 0.9, "high": 0.08},
        },
        "severity": {
            "type": "score",
            "score": 1.31,
            "confidence": 0.7,
            "legend": {"0": "minor", "1": "moderate", "2": "major", "3": "critical"},
            "probabilities": {"0": 0.2, "1": 0.8},
        },
    },
    "usage": {"input_tokens": 67, "output_tokens": 309},
    "extensions": {"backend": "neohorse-vllm-experimental"},
}

MODELS_OK = {
    "object": "list",
    "data": [
        {"id": "neohorse-1.5-flash-preview", "owned_by": "tokenrhythm",
         "context_length": 1048576},
        {"id": "glm-5.3", "owned_by": "tokenrhythm"},
    ],
}

# Fake keys that pass the sk_ shape check, for tests that keep it on.
FAKE_KEY = "sk_tr_" + "a" * 43
FAKE_KEY_2 = "sk_tr_" + "b" * 43


def png_bytes(w: int = 2, h: int = 2) -> bytes:
    """A tiny valid PNG (no Pillow dependency in tests)."""
    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))
    return sig + ihdr + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def make_transport(responder, seen: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responder(request, len(seen))

    return httpx.MockTransport(handler)


def ok_decision(request, n):
    return httpx.Response(200, json=DECISION_OK)


def ok_systemone(request, n):
    return httpx.Response(200, json=SYSTEMONE_OK)


class TestPayload:
    def test_batching_sends_all_questions_and_first_key(self):
        seen: list[httpx.Request] = []
        client = NeoHorseClient(keys=["k1", "k2"], check_keys=False,
                                transport=make_transport(ok_decision, seen))
        client.decide(
            "路口信号灯为红色，前方有行人。",
            [presets.urgency(), presets.score("危险程度？", ["低", "中", "高"], name="risk")],
        )
        body = json.loads(seen[0].content)
        assert body["model"] == "NeoHorse-Jev-4B"
        assert body["state"] == "路口信号灯为红色，前方有行人。"
        assert set(body["questions"]) == {"is_urgent", "risk"}
        assert body["questions"]["is_urgent"]["type"] == "noul"
        assert body["questions"]["risk"]["criteria"] == ["低", "中", "高"]
        assert seen[0].headers["Authorization"] == "Bearer k1"
        assert seen[0].url.path == "/v1/decision"
        assert "image" not in body

    def test_systemone_endpoint_path(self):
        seen: list[httpx.Request] = []
        client = NeoHorseClient(keys=["k1"], check_keys=False, endpoint="systemone",
                                transport=make_transport(ok_systemone, seen))
        client.decide("text", presets.urgency())
        assert seen[0].url.path == "/v1/systemone"

    def test_per_call_endpoint_override(self):
        seen: list[httpx.Request] = []
        client = NeoHorseClient(keys=["k1"], check_keys=False,
                                transport=make_transport(ok_systemone, seen))
        client.decide("text", presets.urgency(), endpoint="systemone")
        assert seen[0].url.path == "/v1/systemone"

    def test_state_can_be_any_json(self):
        seen: list[httpx.Request] = []
        client = NeoHorseClient(keys=["k1"], check_keys=False,
                                transport=make_transport(ok_decision, seen))
        state = {"ticket": "#42", "subject": "payout failure", "attempts": 3}
        client.decide(state, presets.urgency())
        body = json.loads(seen[0].content)
        assert body["state"] == state

    def test_image_goes_top_level_as_data_url(self):
        seen: list[httpx.Request] = []
        client = NeoHorseClient(keys=["k1"], check_keys=False,
                                transport=make_transport(ok_decision, seen))
        client.decide("一只猫", presets.noul("图里有猫吗？"), image=png_bytes())
        body = json.loads(seen[0].content)
        assert body["image"].startswith("data:image/png;base64,")
        assert base64.b64decode(body["image"].split(",", 1)[1])[:4] == b"\x89PNG"
        assert len(body["questions"]) == 1

    def test_normalize_accepts_mapping_single_and_list(self):
        q_map = {"a": {"type": "noul", "instructions": "x"}}
        q_single = {"name": "a", "type": "noul", "instructions": "x"}
        q_list = [{"name": "a", "type": "noul", "instructions": "x"}]
        assert normalize_questions(q_map) == q_map
        assert normalize_questions(q_single) == q_map
        assert normalize_questions(q_list) == q_map

    def test_duplicate_names_get_suffixed(self):
        out = normalize_questions(
            [{"name": "q", "type": "noul", "instructions": "1"},
             {"name": "q", "type": "noul", "instructions": "2"}]
        )
        assert set(out) == {"q", "q_2"}


class TestLimits:
    def test_too_many_questions_rejected_locally(self):
        client = NeoHorseClient(keys=["k1"], check_keys=False,
                                transport=make_transport(ok_decision, []))
        qs = [presets.noul(f"q{i}？") for i in range(17)]
        with pytest.raises(NeoHorseBadRequest, match="1..16"):
            client.decide("text", qs)

    def test_image_with_multiple_questions_rejected(self):
        client = NeoHorseClient(keys=["k1"], check_keys=False,
                                transport=make_transport(ok_decision, []))
        with pytest.raises(NeoHorseBadRequest, match="exactly 1"):
            client.decide("text", [presets.urgency(), presets.severity()], image=png_bytes())

    def test_choice_option_count_limits(self):
        client = NeoHorseClient(keys=["k1"], check_keys=False,
                                transport=make_transport(ok_decision, []))
        with pytest.raises(NeoHorseBadRequest, match="1..255"):
            client.decide("t", presets.choice("q?", {}))

    def test_score_level_count_limits(self):
        client = NeoHorseClient(keys=["k1"], check_keys=False,
                                transport=make_transport(ok_decision, []))
        with pytest.raises(NeoHorseBadRequest, match="2..10"):
            client.decide("t", presets.score("q?", ["only"]))

    def test_non_png_image_rejected(self):
        client = NeoHorseClient(keys=["k1"], check_keys=False,
                                transport=make_transport(ok_decision, []))
        with pytest.raises(NeoHorseBadRequest, match="PNG"):
            client.decide("t", presets.urgency(), image=b"not a png")

    def test_oversized_body_rejected_locally(self):
        client = NeoHorseClient(keys=["k1"], check_keys=False,
                                transport=make_transport(ok_decision, []))
        with pytest.raises(NeoHorseBadRequest, match="MiB"):
            client.decide("x" * (1024 * 1024 + 10), presets.urgency())

    def test_bad_endpoint_rejected(self):
        with pytest.raises(NeoHorseBadRequest, match="endpoint must be"):
            NeoHorseClient(keys=["k1"], check_keys=False, endpoint="chat")


class TestKeyValidation:
    def test_rejects_malformed_key(self):
        with pytest.raises(NeoHorseAuthError, match="not a Token Rhythm API key"):
            NeoHorseClient(keys=["sk_short"])

    def test_rejects_missing_prefix(self):
        with pytest.raises(NeoHorseAuthError):
            NeoHorseClient(keys=["tr_" + "a" * 43])

    def test_accepts_valid_shape_and_strips(self):
        assert check_key_format(" " + FAKE_KEY + " ") == FAKE_KEY

    def test_check_keys_false_allows_fakes(self):
        client = NeoHorseClient(keys=["k1"], check_keys=False,
                                transport=make_transport(ok_decision, []))
        assert client.decide("x", presets.urgency()).request_id is None  # no request_id upstream
        assert client.decide("x", presets.urgency()).answers["is_urgent"].noul == 0.7161


class TestFailover:
    def test_rotates_to_second_key_on_401(self):
        seen: list[httpx.Request] = []

        def responder(request, n):
            if n == 1:
                return httpx.Response(
                    401,
                    json={"code": "UNAUTHORIZED", "message": "未认证或登录已过期",
                          "traceId": "trace_x"},
                )
            return httpx.Response(200, json=DECISION_OK)

        client = NeoHorseClient(keys=["dead", "live"], check_keys=False,
                                transport=make_transport(responder, seen))
        result = client.decide("text", presets.urgency())
        assert result.answers["is_urgent"].noul == 0.7161
        assert len(seen) == 2
        assert seen[0].headers["Authorization"] == "Bearer dead"
        assert seen[1].headers["Authorization"] == "Bearer live"
        assert result.latency_ms is not None
        assert client.usage[client.hint("live")]["calls"] == 1
        assert client.usage[client.hint("dead")]["calls"] == 0

    def test_403_no_grayscale_rotates(self):
        seen: list[httpx.Request] = []

        def responder(request, n):
            if n == 1:
                return httpx.Response(403, json={"code": "FORBIDDEN",
                                                 "message": "未获灰度权限",
                                                 "traceId": "trace_y"})
            return httpx.Response(200, json=DECISION_OK)

        client = NeoHorseClient(keys=["nogray", "ok"], check_keys=False,
                                transport=make_transport(responder, seen))
        assert client.decide("text", presets.urgency()).answers["is_urgent"].noul == 0.7161
        assert len(seen) == 2

    def test_error_carries_code_and_trace_id(self):
        def responder(request, n):
            return httpx.Response(401, json={"code": "UNAUTHORIZED",
                                             "message": "未认证或登录已过期",
                                             "traceId": "trace_abc"})

        client = NeoHorseClient(keys=["dead"], check_keys=False,
                                transport=make_transport(responder, []))
        with pytest.raises(NeoHorseAuthError) as excinfo:
            client.decide("text", presets.urgency())
        assert excinfo.value.code == "UNAUTHORIZED"
        assert excinfo.value.trace_id == "trace_abc"
        assert "traceId trace_abc" in str(excinfo.value)

    def test_429_rotates_when_multiple_keys(self):
        seen: list[httpx.Request] = []

        def responder(request, n):
            if n == 1:
                return httpx.Response(429, json={"code": "RATE_LIMITED",
                                                 "message": "容量限流",
                                                 "traceId": "trace_z"})
            return httpx.Response(200, json=DECISION_OK)

        client = NeoHorseClient(keys=["a", "b"], check_keys=False,
                                transport=make_transport(responder, seen))
        client.decide("text", presets.urgency())
        assert len(seen) == 2  # rotated immediately, no same-key retries
        assert seen[0].headers["Authorization"] == "Bearer a"
        assert seen[1].headers["Authorization"] == "Bearer b"

    def test_all_keys_dead_raises_auth_error(self):
        seen: list[httpx.Request] = []

        def responder(request, n):
            return httpx.Response(401, json={"code": "UNAUTHORIZED",
                                             "message": "未认证或登录已过期"})

        client = NeoHorseClient(keys=["dead1", "dead2"], check_keys=False,
                                transport=make_transport(responder, seen))
        with pytest.raises(NeoHorseAuthError):
            client.decide("text", presets.urgency())
        assert len(seen) == 2

    def test_bad_request_does_not_rotate(self):
        seen: list[httpx.Request] = []

        def responder(request, n):
            return httpx.Response(422, json={"code": "JEV_INVALID_REQUEST",
                                             "message": "JEV 请求参数不符合协议",
                                             "traceId": "trace_422"})

        client = NeoHorseClient(keys=["k1", "k2"], check_keys=False,
                                transport=make_transport(responder, seen))
        with pytest.raises(NeoHorseBadRequest, match="JEV 请求参数"):
            client.decide("text", presets.urgency())
        assert len(seen) == 1  # no rotation on payload errors


class TestRetries:
    def test_500_retried_on_same_key_then_succeeds(self):
        seen: list[httpx.Request] = []

        def responder(request, n):
            if n == 1:
                return httpx.Response(502, json={"code": "BAD_GATEWAY",
                                                 "message": "上游故障"})
            return httpx.Response(200, json=DECISION_OK)

        client = NeoHorseClient(keys=["k1", "k2"], check_keys=False, backoff_initial=0,
                                transport=make_transport(responder, seen))
        result = client.decide("text", presets.urgency())
        assert result.answers["is_urgent"].noul == 0.7161
        assert len(seen) == 2
        assert seen[0].headers["Authorization"] == seen[1].headers["Authorization"] == "Bearer k1"

    def test_network_error_retried_then_rotates(self):
        seen: list[httpx.Request] = []

        def responder(request, n):
            if n <= 3:  # max_retries=2 -> 3 attempts on k1, all network errors
                raise httpx.ConnectError("refused")
            return httpx.Response(200, json=DECISION_OK)

        client = NeoHorseClient(keys=["k1", "k2"], check_keys=False, backoff_initial=0,
                                transport=make_transport(responder, seen))
        result = client.decide("text", presets.urgency())
        assert result.answers["is_urgent"].noul == 0.7161
        assert len(seen) == 4  # 3 failed attempts on k1, 1 on k2
        assert seen[0].headers["Authorization"] == "Bearer k1"
        assert seen[3].headers["Authorization"] == "Bearer k2"

    def test_single_key_429_is_retried_with_retry_after(self):
        seen: list[httpx.Request] = []

        def responder(request, n):
            if n == 1:
                return httpx.Response(429, headers={"Retry-After": "0"},
                                      json={"code": "RATE_LIMITED", "message": "容量限流"})
            return httpx.Response(200, json=DECISION_OK)

        client = NeoHorseClient(keys=["only"], check_keys=False, backoff_initial=0,
                                transport=make_transport(responder, seen))
        client.decide("text", presets.urgency())
        assert len(seen) == 2  # same key retried, not an auth failure
        assert seen[0].headers["Authorization"] == seen[1].headers["Authorization"] == "Bearer only"

    def test_5xx_exhausted_across_all_keys_raises(self):
        seen: list[httpx.Request] = []

        def responder(request, n):
            return httpx.Response(503, json={"code": "UNAVAILABLE", "message": "down"})

        client = NeoHorseClient(keys=["k1", "k2"], check_keys=False, max_retries=1,
                                backoff_initial=0,
                                transport=make_transport(responder, seen))
        with pytest.raises(Exception, match="503"):
            client.decide("text", presets.urgency())
        assert len(seen) == 4  # 2 attempts per key


class TestModels:
    def test_models_get_request_shape(self):
        seen: list[httpx.Request] = []

        def responder(request, n):
            assert request.method == "GET"
            return httpx.Response(200, json=MODELS_OK)

        client = NeoHorseClient(keys=["k1"], check_keys=False,
                                transport=make_transport(responder, seen))
        result = client.models()
        assert [m.name for m in result.models] == ["neohorse-1.5-flash-preview", "glm-5.3"]
        assert result.models[0].owned_by == "tokenrhythm"
        assert result.latency_ms is not None
        assert seen[0].url.path == "/v1/models"


class TestUsage:
    def test_decision_tokens_recorded_from_top_level(self):
        client = NeoHorseClient(keys=["k1"], check_keys=False,
                                transport=make_transport(ok_decision, []))
        r = client.decide("text", presets.urgency())
        assert r.usage.input_tokens == 26
        assert r.usage.output_tokens is None
        assert client.usage[client.hint("k1")] == {
            "calls": 1, "input_tokens": 26, "output_tokens": 0,
        }

    def test_decision_falls_back_to_expanded_tokens(self):
        body = {k: v for k, v in DECISION_OK.items() if k != "input_tokens"}
        client = NeoHorseClient(keys=["k1"], check_keys=False,
                                transport=make_transport(lambda r, n: httpx.Response(200, json=body), []))
        r = client.decide("text", presets.urgency())
        assert r.usage.input_tokens == 26  # from input_details.expanded_tokens

    def test_systemone_usage_recorded(self):
        client = NeoHorseClient(keys=["k1"], check_keys=False, endpoint="systemone",
                                transport=make_transport(ok_systemone, []))
        r = client.decide("text", presets.urgency())
        assert r.usage.input_tokens == 67
        assert r.usage.output_tokens == 309
        assert client.usage[client.hint("k1")]["output_tokens"] == 309

    def test_image_tokens_flow_into_input_details(self):
        seen: list[httpx.Request] = []
        body = dict(DECISION_OK)
        body["input_details"] = {"input_tokens": 90, "expanded_tokens": 90,
                                 "question_count": 1, "image_tokens": 64}
        client = NeoHorseClient(keys=["k1"], check_keys=False,
                                transport=make_transport(
                                    lambda r, n: httpx.Response(200, json=body), seen))
        r = client.decide("一只猫", presets.noul("图里有猫吗？"), image=png_bytes())
        assert r.input_details["image_tokens"] == 64


class TestCache:
    def test_identical_calls_hit_cache_once(self):
        seen: list[httpx.Request] = []
        client = NeoHorseClient(keys=["k1"], check_keys=False,
                                transport=make_transport(ok_decision, seen))
        r1 = client.decide("same text", presets.urgency())
        r2 = client.decide("same text", presets.urgency())
        assert r1 == r2
        assert len(seen) == 1
        # mutating a returned result must not poison the cache
        r1.answers["is_urgent"].noul = 999.0
        r3 = client.decide("same text", presets.urgency())
        assert r3.answers["is_urgent"].noul == 0.7161

    def test_different_state_misses_cache(self):
        seen: list[httpx.Request] = []
        client = NeoHorseClient(keys=["k1"], check_keys=False,
                                transport=make_transport(ok_decision, seen))
        client.decide("text a", presets.urgency())
        client.decide("text b", presets.urgency())
        assert len(seen) == 2

    def test_different_image_misses_cache(self):
        seen: list[httpx.Request] = []
        client = NeoHorseClient(keys=["k1"], check_keys=False,
                                transport=make_transport(ok_decision, seen))
        q = presets.noul("图里有猫吗？")
        client.decide("一只猫", q, image=png_bytes())
        client.decide("一只猫", q, image=png_bytes(4, 4))
        assert len(seen) == 2

    def test_cache_disabled(self):
        seen: list[httpx.Request] = []
        client = NeoHorseClient(keys=["k1"], check_keys=False, cache_size=0,
                                transport=make_transport(ok_decision, seen))
        client.decide("same", presets.urgency())
        client.decide("same", presets.urgency())
        assert len(seen) == 2


class TestPresets:
    def test_shapes(self):
        u = presets.urgency()
        assert u.type == "noul" and u.name == "is_urgent"
        s = presets.severity()
        assert s.type == "score"
        assert s.criteria == ["minor", "moderate", "major", "critical"]
        assert presets.sentiment().criteria == ["negative", "neutral", "positive"]

    def test_noul_criteria(self):
        q = presets.noul("Refund?", true="wants money back", false="no refund request")
        assert q.criteria == NoulCriteria(true="wants money back", false="no refund request")
        assert presets.noul("Refund?").criteria is None

    def test_choice_mapping_format(self):
        q = presets.choice(
            "What is this about?",
            {"billing": "payment issues", "technical": None},
        )
        assert q.type == "choice"
        assert q.criteria == {"billing": "payment issues", "technical": None}

    def test_priority_preset(self):
        p = presets.priority()
        assert p.type == "choice"
        assert set(p.criteria) == {"low", "medium", "high", "urgent"}


class TestAsync:
    def test_async_client_failover_and_cache(self):
        import asyncio

        seen: list[httpx.Request] = []

        def responder(request, n):
            if n == 1:
                return httpx.Response(429, json={"code": "RATE_LIMITED",
                                                 "message": "容量限流"})
            return httpx.Response(200, json=DECISION_OK)

        async def run():
            client = AsyncNeoHorseClient(keys=["a", "b"], check_keys=False,
                                         backoff_initial=0,
                                         transport=make_transport(responder, seen))
            r1 = await client.decide("text", presets.urgency())
            r2 = await client.decide("text", presets.urgency())
            return r1, r2

        r1, r2 = asyncio.run(run())
        assert r1.answers["is_urgent"].noul == 0.7161
        assert r2 == r1
        assert len(seen) == 2  # 429 rotate + first real call; cache serves the rest

    def test_async_models(self):
        import asyncio

        seen: list[httpx.Request] = []

        async def run():
            client = AsyncNeoHorseClient(keys=["k1"], check_keys=False,
                                         transport=make_transport(
                                             lambda r, n: httpx.Response(200, json=MODELS_OK), seen))
            return await client.models()

        result = asyncio.run(run())
        assert len(result.models) == 2
