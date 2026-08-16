"""Shorten an over-long worker question into something the user can act on.

Neo keeps receiving the question in full — its judgement is only as good as what it
reads. This module is about the OTHER reader: the human opening `/neo` to review what
Neo did. A worker that pastes a 7,000-character feature-order brief into
`jarvis wo ask` produces a page nobody finishes, and a review nobody finishes is a
review that did not happen.

So the dashboard renders a DIGEST — one model call, cheap model, shaped by the vendored
`i-have-adhd` output style (`assets/digest/`, MIT, see the README there) — and keeps the
verbatim text one disclosure away.

THREE PROPERTIES THIS MODULE EXISTS TO HOLD.

**The digest is display, never input.** Nothing here is passed to Neo, to a worker, or
into a learning. It is computed after the question has already been answered and is read
only by the dashboard, so a bad digest costs the user a confusing paragraph, never a
wrong decision. That is what makes a formatting-grade model the right call.

**The shape is guaranteed by the validator, not by the prompt.** The user asked for
bullets, options and a highlighted recommendation. Asking a model for markdown and
hoping gets you prose with dashes in it about a third of the time; asking for strict
JSON through `structured.request` and validating the fields means the template can rely
on them existing. It also means the page renders no model-authored HTML — the fields go
through Jinja's autoescaping as text.

**A missing digest is a working page.** Every failure mode here ends with the raw
question rendered exactly as it is today. There is no state in which the user is shown a
digest INSTEAD of a question they cannot get back to.
"""

from __future__ import annotations

import json
from functools import lru_cache, partial
from typing import Any, Callable

from . import claude_cli, structured
from .bootstrap import ASSETS

#: The vendored output style. DELIBERATELY NOT under `assets/agents/` or
#: `assets/skills/`: `bootstrap._rebuild` copytrees both of those into every project, so
#: a prompt dropped there becomes a subagent or a worker skill. See the README beside it.
SKILL_PATH = ASSETS / "digest" / "i-have-adhd.SKILL.md"

#: First line of the digest system prompt. Machine-readable on purpose, exactly like
#: `panel.SEAT_HEADER`: it tells anything reading the call (the test fake keys on it)
#: which kind of call this is, without depending on the vendored prose.
DIGEST_HEADER = "# Jarvis dashboard digest"

#: Below this many characters a question is already readable and gets no digest. Every
#: digest is a real model call, so the threshold is what keeps a one-line question from
#: costing one. Questions on the `/neo` page are typically 100-400 characters; the one
#: that prompted this feature (#53) was ~7,000.
MIN_CHARS = 800

#: Caps, enforced in the validator rather than only asked for in the prompt. The skill's
#: own rule 9 is "cap lists at 5 items"; a model that ignores it must not be able to put
#: twelve bullets on the page, because twelve bullets is the problem this feature exists
#: to remove.
MAX_BULLETS = 5
MAX_OPTIONS = 5

#: How much of a long field survives. Generous — this is a guard against a model that
#: pasted the question back, not a style rule.
MAX_FIELD_CHARS = 600

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


#: The transport. Tools stripped: the digest must be a rewriting of the text it was
#: handed and nothing else. A tooled callee asked to summarise a question about
#: `src/jarvis/panel.py` will go and READ `src/jarvis/panel.py`, and then the page shows
#: the reader a description of the code instead of a shortening of the question — a
#: failure mode that looks like a good answer, which is the worst kind.
CALL = partial(claude_cli.run_headless_result, tools="")


class DigestError(RuntimeError):
    """A digest could not be produced for this question."""


@lru_cache(maxsize=1)
def load_skill() -> str:
    """The vendored output style, verbatim. Cached: a file on disk that only changes
    when the build does."""
    if not SKILL_PATH.is_file():
        raise DigestError(f"the digest output style does not ship in this build ({SKILL_PATH})")
    return SKILL_PATH.read_text().strip()


@lru_cache(maxsize=1)
def build_system_prompt() -> str:
    """Header, then the vendored style verbatim, then what to emit.

    Byte-stable across every question, like Neo's own prompt and for the same reason:
    consecutive digests in one daemon pass share a cached prefix.
    """
    return "\n\n".join([DIGEST_HEADER, load_skill(), INSTRUCTIONS.strip()])


def _text_field(value: Any) -> str:
    return str(value or "").strip()[:MAX_FIELD_CHARS]


def _list_field(value: Any, cap: int) -> list[str]:
    """A list of non-empty one-liners, capped.

    A model that answers with a bare string instead of a list is not making a mistake
    worth throwing an otherwise-good digest away for: wrap it. A model that answers with
    a list of dicts is, so `str()` never runs on one — the entry is dropped.
    """
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out = [_text_field(v) for v in value if isinstance(v, (str, int, float))]
    return [v for v in out if v][:cap]


def validate(data: dict[str, Any]) -> dict[str, Any]:
    """Normalise a reply into the four fields the template renders.

    Only `headline` is required, and it is required strictly: a digest whose first line
    is empty is a blank box where the question used to be. Everything else degrades to
    empty, because a question with no options really does have no options.
    """
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
              on_usage: Callable[[Any], None] | None = None) -> dict[str, Any]:
    """One digest for one question. Raises rather than returning a broken shape.

    Two attempts: unlike Neo's answering path — which must fail SAFE, turning garbage
    into an escalation the user sees — there is nothing safe to fall back to here, and
    nothing is riding on the second call but a nicer paragraph. `on_invalid=None` lets
    the failure reach the caller, which records it and moves on (see
    `Daemon._digest_batch`).
    """
    from .paths import ensure_home

    return structured.request(
        question,
        validate=validate,
        system_prompt=build_system_prompt(),
        model=model,
        attempts=2,
        timeout=timeout,
        # Neutral cwd, for the same reason Neo uses one: running from a project
        # directory would pull that repo's CLAUDE.md into the prompt.
        cwd=ensure_home(),
        call=call,
        # A digest is an extra call per question that the user never asked for, so what
        # it costs belongs on the work order's bill beside the answer it shortens.
        on_usage=on_usage,
    )


def needs_digest(question: str, min_chars: int = MIN_CHARS) -> bool:
    """Is this question long enough to be worth a call?"""
    return len(question or "") >= min_chars


# -- storage encoding ---------------------------------------------------------------
#
# The digest lives in one TEXT column as JSON. It is a display artefact with no
# queryable structure — nothing ever selects on a bullet — so a table of its own would
# buy an extra join and a second cascade to keep in step with `questions`.


def encode(view: dict[str, Any]) -> str:
    return json.dumps(view, sort_keys=True)


def encode_failure(reason: str) -> str:
    """A recorded failure, so it is not retried for ever.

    The column is NULL until a digest has been ATTEMPTED, and the daemon only picks up
    NULLs, so a failure must write something or every tick would spend another call on
    the same question. One attempt per question, ever: the cost of not retrying is a
    question that renders in full — which is exactly today's behaviour — and the cost of
    retrying is an unbounded spend the user never asked for.
    """
    return json.dumps({"error": str(reason)[:500]})


def decode(raw: str | None) -> dict[str, Any] | None:
    """The stored digest, or None when there is nothing renderable.

    None covers all three of "not attempted yet", "attempted and failed" and "the row
    predates this feature", and the template treats them identically: show the question.
    """
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
    """Why the digest for this row is missing, for a log or a debug surface. Empty when
    it was never attempted."""
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return ""
    return str(data.get("error", "")) if isinstance(data, dict) else ""


def skill_attribution() -> str:
    """One line naming what shaped the digest, for the page footer.

    The style is someone else's work under a permissive licence; a surface built out of
    it says so.
    """
    return ("shortened with the i-have-adhd output style "
            "(github.com/ayghri/i-have-adhd, MIT)")
