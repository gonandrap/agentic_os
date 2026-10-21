"""Shorten an over-long worker question into something the user can act on"""

from __future__ import annotations

import json
from functools import lru_cache, partial
from typing import Any, Callable

from . import claude_cli, structured
from .bootstrap import ASSETS
from .catalog import DEFAULT_DIGEST_MAX_QUESTION_CHARS

#: The vendored output style
SKILL_PATH = ASSETS / "digest" / "i-have-adhd.SKILL.md"

#: First line of the digest system prompt
DIGEST_HEADER = "# Jarvis dashboard digest"

#: Below this many characters a question is already readable and gets no digest (#53)
MIN_CHARS = 800

#: Caps, enforced in the validator rather than only asked for in the prompt
MAX_BULLETS = 5
MAX_OPTIONS = 5

#: How much of a long field survives
MAX_FIELD_CHARS = 600

#: How much of the question the model is SHOWN (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)
MAX_QUESTION_CHARS = DEFAULT_DIGEST_MAX_QUESTION_CHARS

#: The clip is LABELLED and the label is INSIDE the prompt the model reads
CLIP_NOTICE = ("\n\n[clipped for this summary: {cut} characters of the question were "
               "cut here. The full question is on the work-order record — summarise "
               "only what you were shown, and say it is partial.]")


def clip(question: str, max_chars: int = MAX_QUESTION_CHARS) -> str:
    """The question as the digest model sees it: whole, or clipped and labelled"""
    if len(question) <= max_chars:
        return question
    return question[:max_chars] + CLIP_NOTICE.format(cut=len(question) - max_chars)

INSTRUCTIONS = f"""
# Your task here

You are shortening ONE question that an autonomous worker agent asked the user's
delegate. It has already been answered; the user is now reading it to review that
answer. They will not read the original — it is too long, which is why you are here.

Apply the style above to the question's CONTENT. You are not answering the question and
you are not judging it. You are making it possible to read.

Output STRICT JSON, nothing else, with exactly these keys:

{{"headline": "<one line: what is actually being asked, in the user's terms>",
 "bullets": ["<what the reader must know to decide>", ...],
 "options": ["<option — its one-line trade-off>", ...],
 "recommendation": "<what the asker recommended, or what the reader should do>"}}

- `headline` is required and is one sentence. Everything else may be an empty list or
  an empty string when the question genuinely has no such content — an invented option
  is worse than no options.
- At most {MAX_BULLETS} bullets and {MAX_OPTIONS} options, ranked, most important first.
- `options` is for a question that asks the reader to CHOOSE. Leave it empty otherwise.
- `recommendation` is what the asker already recommended, if they did. Do not invent one.
- Plain text in every field: no markdown, no bullet characters, no numbering. The page
  does the formatting.
- Preserve identifiers verbatim — work order ids, file paths, command names, numbers.
  Those are what the reader searches for.
"""


#: The transport
CALL = partial(claude_cli.run_headless_result, tools="")


class DigestError(RuntimeError):
    """A digest could not be produced for this question"""


@lru_cache(maxsize=1)
def load_skill() -> str:
    """The vendored output style, verbatim"""
    if not SKILL_PATH.is_file():
        raise DigestError(f"the digest output style does not ship in this build ({SKILL_PATH})")
    return SKILL_PATH.read_text().strip()


@lru_cache(maxsize=1)
def build_system_prompt() -> str:
    """Header, then the vendored style verbatim, then what to emit"""
    return "\n\n".join([DIGEST_HEADER, load_skill(), INSTRUCTIONS.strip()])


def _text_field(value: Any) -> str:
    return str(value or "").strip()[:MAX_FIELD_CHARS]


def _list_field(value: Any, cap: int) -> list[str]:
    """A list of non-empty one-liners, capped"""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out = [_text_field(v) for v in value if isinstance(v, (str, int, float))]
    return [v for v in out if v][:cap]


def validate(data: dict[str, Any]) -> dict[str, Any]:
    """Normalise a reply into the four fields the template renders"""
    headline = _text_field(data.get("headline"))
    if not headline:
        raise structured.InvalidOutput("the digest has no `headline`")
    return {
        "headline": headline,
        "bullets": _list_field(data.get("bullets"), MAX_BULLETS),
        "options": _list_field(data.get("options"), MAX_OPTIONS),
        "recommendation": _text_field(data.get("recommendation")),
    }


def summarise(question: str, *, model: str, timeout: int = 120,
              call: Callable[..., Any] = CALL,
              max_chars: int = MAX_QUESTION_CHARS,
              on_usage: Callable[[Any], None] | None = None) -> dict[str, Any]:
    """One digest for one question"""
    from .paths import ensure_home

    return structured.request(
        # Clipped and labelled before it becomes the prompt — see `clip`
        clip(question, max_chars),
        validate=validate,
        system_prompt=build_system_prompt(),
        model=model,
        attempts=2,
        timeout=timeout,
        # Neutral cwd, for the same reason Neo uses one: running from a project directory would pull that...
        cwd=ensure_home(),
        call=call,
        # Declared only when a recorder exists: `on_usage` is optional here
        records_itself="digest" if on_usage is not None else "",
        # A digest is an extra call per question that the user never asked for
        on_usage=on_usage,
    )


def needs_digest(question: str, min_chars: int = MIN_CHARS) -> bool:
    """Is this question long enough to be worth a call?"""
    return len(question or "") >= min_chars


# -- storage encoding


def encode(view: dict[str, Any]) -> str:
    return json.dumps(view, sort_keys=True)


def encode_failure(reason: str) -> str:
    """A recorded failure, so it is not retried for ever"""
    return json.dumps({"error": str(reason)[:500]})


def decode(raw: str | None) -> dict[str, Any] | None:
    """The stored digest, or None when there is nothing renderable"""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("headline"):
        return None
    return data


def failure_reason(raw: str | None) -> str:
    """Why the digest for this row is missing, for a log or a debug surface"""
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return ""
    return str(data.get("error", "")) if isinstance(data, dict) else ""


def skill_attribution() -> str:
    """One line naming what shaped the digest, for the page footer"""
    return ("shortened with the i-have-adhd output style "
            "(github.com/ayghri/i-have-adhd, MIT)")
