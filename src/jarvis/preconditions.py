"""The stop-condition phrase family, and nothing else.

§2.4 of docs/superpowers/specs/2026-10-15-a-precondition-is-a-row-not-prose.md. A LEAF
module importing nothing from `jarvis` — the `timeline.py` and `gaps.py` tier — because
three unrelated callers need the same answer: the creation refusal (`ops`), the detector
(`invariants`) and the tests. A second copy of the phrase list is how the refusal and the
detector come to disagree about what a hold is.

**Precision over recall, deliberately.** The predicate gates a REFUSAL of a human's
command, and a false positive makes a legitimate order unfileable until the operator
argues with a regex. Accepted false negatives, named so nobody "improves" them in: `wait
for X first`, `after Y ships`, `once Z lands`, `blocked on`, `needs A`. Those read as
sequencing inside a brief as often as they read as a stop condition, and the cost of the
miss is bounded — the detector fires on nothing and the order runs, which is today's
behaviour. The cost of the over-match is an operator who cannot file.
"""

from __future__ import annotations

#: The five, and only the five — §4: a sixth is a one-line diff behind the posture above,
#: and adding one speculatively is how precision is lost.
STOP_PHRASES = ("do not start until", "do not start before",
                "do not dispatch until", "held until", "blocked until")

#: What may sit in front of a phrase without changing what it says: a bullet, a quote
#: marker, a bold run.
_MARKERS = "-*•>#\t "

#: How long a matched sentence may be where it is quoted back. Clipped, with no ellipsis
#: games: the refusal message quotes the user's own words, so a truncation that looked
#: like the OS editing them would be worse than a long line.
MAX_SENTENCE = 300


class PreconditionError(ValueError):
    """A ref that names nothing. The one refusal this module makes."""


def _candidate_starts(line: str) -> list[int]:
    """Offsets a stop phrase may begin at: the start of the line, or after `.`/`;`/`:`."""
    starts = [0]
    for i, ch in enumerate(line):
        if ch in ".;:":
            starts.append(i + 1)
    return starts


def states_hold(title: str, description: str) -> str | None:
    """The matched stop-condition sentence, or None.

    Case-insensitive; the phrase must begin a LINE or follow `.`/`;`/`:`, optionally
    behind a bullet or `**` marker; and there must be a non-empty remainder on the same
    line — `HELD until` with nothing after it names no hold.
    """
    for line in f"{title}\n{description}".splitlines():
        lowered = line.lower()
        for start in _candidate_starts(line):
            at = start
            while at < len(line) and line[at] in _MARKERS:
                at += 1
            for phrase in STOP_PHRASES:
                if not lowered.startswith(phrase, at):
                    continue
                remainder = line[at + len(phrase):].strip(_MARKERS + ":")
                if remainder:
                    return line[at:].strip(_MARKERS + "*")[:MAX_SENTENCE]
    return None


def checked_ref(raw: str) -> str:
    """The ref, stripped. The ONLY refusal is an empty string.

    No membership check and no existence check: an unresolved `bl-` id is a finding about
    the OS's records, not an error — the `--ref` rule `jarvis io create` already ships.
    """
    ref = (raw or "").strip()
    if not ref:
        raise PreconditionError(
            "a precondition needs something to point at — `bl-…`, `kn-…`, `io-…`, "
            "`#issue`, a URL, or the words for the wait")
    return ref
