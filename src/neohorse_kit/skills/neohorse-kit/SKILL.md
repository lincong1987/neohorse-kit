---
name: neohorse-kit
description: Judge text or JSON with the NeoHorse JEV decision API (Token
  Rhythm) — probability-based yes/no scores (noul), label routing (choice), ordered
  ratings (score), and single-question PNG image judging; presets for
  urgency/sentiment/severity/priority. Use when scoring, routing, triaging,
  grading or deciding on text, JSON records, or one image.
---

# neohorse-kit — NeoHorse JEV decisions for agents

Prefer the MCP tools (`decide`, `ask`, `list_models`) when they are available
in this session. Otherwise use the CLI or Python below.

## Setup (once)

- Needs `NEOHORSE_API_KEYS` (comma-separated key pool) or `NEOHORSE_API_KEY`
  in the environment. Keys look like `sk_...`; get one from the Token Rhythm
  console (https://tokenrhythm.studio). The JEV model is grayscale-gated: a
  key without access answers 403.
- No install needed: `uvx neohorse-kit ...`. Persistent: `pip install neohorse-kit`.

## Judging via CLI

```bash
uvx neohorse-kit "路口信号灯为红色，前方有行人。"                      # urgency preset (noul 0..1)
uvx neohorse-kit "TEXT" --ask "现在是否应停车？" --kind noul
uvx neohorse-kit "TEXT" --ask "危险程度？" --kind score --levels 低 中 高
uvx neohorse-kit "TEXT" --ask "应该走哪条路线？" --kind choice --options left:"左转" straight:"直行"
uvx neohorse-kit photo.png --image --ask "这张图片里有猫吗？"          # one PNG, one question
uvx neohorse-kit --models                                              # key health check
```

Output JSON: `answers.<name>.noul` / `.choice` / `.score`, with
`probabilities` and (except noul) `confidence`.

## Choosing the question type

- Yes/no decision → `noul`. Ask one concrete question — "现在是否应停车？",
  never "分析这段文本".
- Routing among labels → `choice` (1..255 options); `criteria` maps each label
  to a short description.
- Ordered intensity → `score` (2..10 levels); `criteria` lists levels from low
  to high.

## Limits (the API answers 422 when exceeded)

- 1..16 questions per text call; image calls take exactly 1 question.
- `image` is a top-level PNG base64 data URL; body ≤ 1 MiB (≤ 8 MiB with an
  image). Pass a path or bytes and neohorse-kit converts it.
- JEV is a decision model: never call it through `/v1/chat/completions`.

## Batching (Python — one upstream call for many questions)

```python
from neohorse_kit import NeoHorseClient, presets

client = NeoHorseClient()
r = client.decide(state, [presets.urgency(), presets.priority()])
r.answers["priority"].choice           # typed answers, IDE-completable
```

Custom questions:

```python
from neohorse_kit.types import Choice, Score
Choice(instructions="Which lane?", criteria={"left": "turn", "straight": None})
Score(instructions="Risk?", criteria=["low", "medium", "high"])
```

`state` may be any JSON object, not just text — pass structured records
directly. `client.decide(state, q, endpoint="systemone")` switches to the
System One compatible response shape.

## Reading results

Probabilities are not verdicts, and the platform docs say explicitly they are
not calibrated correctness rates: pick a threshold that suits the task (e.g.
`noul > 0.8` means act) instead of trusting a single answer blindly.
`/v1/decision` reports packed `input_tokens`; `/v1/systemone` reports
`usage.input_tokens` / `usage.output_tokens`.

## Cost care

Identical requests are served from an LRU cache. Pass `--no-cache` (CLI) or
`cache_size=0` (Python) when you need a genuinely fresh call.
