"""Neo deciding a work order's pending assumptions, so the last human step can go (docs/superpowers/specs/2026-09-15-neo-decides-an-assumption.md)"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Collection
from dataclasses import dataclass
from itertools import zip_longest
from typing import Any

from . import specs
from .catalog import (
    DEFAULT_VALIDATION_ASSUMPTION_SPEC_CHARS,
    DEFAULT_VALIDATION_DECISION_RECORD_CHARS,
)
from .stakes import Stakes

log = logging.getLogger(__name__)

#: The Neo question kind
QUESTION_KIND = "assumption"

#: A question id the assumption's own text names
_CITED_QUESTION_RE = re.compile(
    r"\b(?:neo\s+question|question|neo|q)\s*#?\s*(\d{1,6})\b", re.IGNORECASE)

#: A SIBLING assumption the text names. Keyword-anchored, because a bare `#n` must match
#: NOTHING: bodies routinely carry `issue #832` and `PR #1090` (spec §4a).
_CITED_SIBLING_RE = re.compile(r"\bassumptions?\b", re.IGNORECASE)
_CITED_SIBLING_TAIL_RE = re.compile(r"\s*(?:,|and)?\s*#?\s*(\d{1,6})\b", re.IGNORECASE)

#: How many cited ids one packet quotes
_CITED_LIMIT = 5

#: How many answered rows the record PULLS (spec §3)
_ANSWERED_ROWS = 200

#: `assumptions.decided_by` for a verdict this module reached
DECIDER = "neo"

STAKES_ROUTINE = "routine"
STAKES_HIGH = "high"
#: What the record calls a `stakes` the reviewer never gave
STAKES_UNCLASSIFIED = "unclassified"

#: **AN ALLOWLIST, NOT A BLOCKLIST, AND THAT IS THE WHOLE POINT** (kn-32434cef's shape)
ROUTINE_STAKES = frozenset({STAKES_ROUTINE})

#: WHY THE OS DID NOT DECIDE THIS ASSUMPTION, as a stable token
HELD_DISABLED = "disabled"
HELD_STATUS = "status"
HELD_SETTLED = "settled"
HELD_PANEL_GAVE_UP = "panel_gave_up"
HELD_REFUSAL_UNANSWERED = "refusal_unanswered"

#: The two sentences that code renders, and which one depends on whether anything moved (docs/superpowers/specs/2026-09-28-stale-blockers-outlive-what-settled-them.md)
REFUSAL_UNANSWERED_REASON = ("you refused an assumption on this work order and the "
                             "worker has not delivered again since")
#: Issue #975: the held reason a user reads must end in something they can type
REFUSAL_UNDECLARED_REASON = ("you refused an assumption on this work order, and the "
                             "worker has pushed commits since without running `jarvis "
                             "wo finish` — the OS has asked it to declare them; you can "
                             "also send it a message asking it to finish, or run `jarvis "
                             "validation force` to judge the pull request as it stands")
HELD_ASKED = "asked"
HELD_HIGH_STAKES = "high_stakes"
#: What the RUNNING pass needs and the parked one cannot reach: an assumption this pass has already...
HELD_JUDGED = "judged_early"

#: The confirmation pass's own five (spec §7), beside the seven `decide` already has (docs/superpowers/specs/2026-10-14-neo-never-reads-a-diff.md)
HELD_UNJUDGED = "unjudged"
HELD_OBJECTED = "objected"
HELD_CONFIRMING = "confirming"
HELD_OBJECTION_IN_FLIGHT = "objection_in_flight"
HELD_EVIDENCE_SECRET = "evidence_secret"

#: docs/superpowers/specs/2026-09-28-a-dropped-confirmation-must-not-hold-an-assumption- for-ever.md...
HELD_ROUND_OPEN = "round_open"
HELD_CONFIRM_SPENT = "confirm_spent"

#: WHICH DROPPED CONFIRMATION MAY BE ASKED AGAIN — the settle site's allowlist (§4.1)
TRANSIENT_DROPS = frozenset({HELD_STATUS, HELD_REFUSAL_UNANSWERED,
                             HELD_OBJECTION_IN_FLIGHT})

#: A round's outcome that means the panel has NOT finished with it
_UNRESOLVED_OUTCOMES = ("", "pending")

#: THE STATUS EACH PASS ACTS IN, and the conditions below read these rather than a literal of their...
REVIEW_PASS_STATUS = "needs_review"
EARLY_PASS_STATUS = "running"
REVIEW_PASS_STATUSES = (REVIEW_PASS_STATUS, EARLY_PASS_STATUS)

#: `provisional_verdict` values §5 writes and this module reads back
PROVISIONAL_ACCEPT = "accept"
PROVISIONAL_OBJECT = "object"

#: THE FIRST NET, and it is deliberately not a taste filter (#713)
_DATA_OBJECTS = (r"tables?|columns?|databases?|db|indexe?s?|rows?|records?|data|"
                 r"collections?|buckets?|volumes?")
#: Up to two words may sit between the verb and its object
_NOT_A_PREPOSITION = (r"(?:(?!from\b|in\b|into\b|out\b|off\b|on\b|to\b|by\b|for\b|at\b|"
                      r"when\b|during\b|because\b|so\b)\w+\s+){0,2}")

_HS_CREDENTIAL = r"credential|secret|password|\bapi[ -]?key\b|\btoken\b|\bauth\b"
# `live` only as a live THING; `production`/`prod` name it on their own
_HS_PRODUCTION = (r"\bproduction\b|\bprod\b"
                  r"|\blive\s+(?:credential|key|secret|token|data|database|db|"
                  r"environment|env|traffic|system|server|fleet|user|account|customer|"
                  r"instance|deployment|service)"
                  r"|\bgo(?:es|ing|ne)?\s+live\b")
# `drop` only with a data object AFTER it — "dropped from the record" is not one
_HS_DESTRUCTIVE = (r"\bdelet|\bdestroy|\btruncat|\birreversib|\bpurge"
                   r"|\bdrop(?:s|ped|ping)?\s+(?:(?:the|a|an|all|this|these|its)\s+)?"
                   + _NOT_A_PREPOSITION + rf"(?:{_DATA_OBJECTS})\b")
# The noun `migration` IS the act
_HS_SCHEMA = (r"|\bmigrat(?:e|es|ed|ing)\s+(?:(?:the|a|an|all)\s+)?(?:\w+\s+){0,2}"
              r"(?:database|db|schema|tables?|data|rows?|users?|production|prod)\b"
              r"|\bschema\s+(?:change|migration|edit|rewrite)|\bchange\s+the\s+schema\b"
              r"|\balter\s+(?:the\s+)?(?:table|column|schema)|\bALTER\s+TABLE\b"
              r"|\b(?:add|adds|adding|drop|drops|dropping|rename|renames|renaming)\s+"
              r"(?:a|the)\s+column\b")
_HS_MIGRATION = r"\bmigrations?\b|\bbackfill" + _HS_SCHEMA
_HS_MONEY = r"\bbill(ed|ing|s)?\b|\bprice|\bpricing\b|\binvoice|\bcharge[ds]?\b|\bspend"
# Shipping something somewhere — not the noun "the next release"
_HS_SHIPPING_TAIL = (r"|\brelease\w*\s+(?:\S+\s+){0,3}?to\s+(?:prod|production|users?|"
                     r"customers?|the\s+fleet|pypi|npm)\b"
                     r"|\b(?:ship|ships|shipped|shipping|push|pushes|pushed|pushing)\s+"
                     r"(?:\S+\s+){0,3}?to\s+(?:prod|production|users?|customers?|"
                     r"the\s+fleet|main|master|pypi|npm)\b")
_HS_SHIPPING = (r"\bpublish\w*\s+(?:\S+\s+){0,3}?to\b|\bdeploy\w*\s+(?:\S+\s+){0,3}?to\b"
                r"|\b(?:cut|cuts|cutting|ship|ships|shipped|shipping|make|makes|making)"
                r"\s+(?:a|the|another)\s+release\b" + _HS_SHIPPING_TAIL)
_HS_PII = r"\bpii\b|\bgdpr\b|personal data|\bpersonally identifiable"
_HS_LEGAL = r"\blicen[cs]e|\blegal\b|\bcopyright\b"
_HS_BREAKING = r"breaking change|backward(s)? incompatible"

HIGH_STAKES = (
    _HS_CREDENTIAL,
    _HS_PRODUCTION,
    _HS_DESTRUCTIVE,
    _HS_MIGRATION,
    _HS_MONEY,
    _HS_SHIPPING,
    _HS_PII,
    _HS_LEGAL,
    _HS_BREAKING,
)

#: THE ONE CARVE-OUT, and it is a sense of a word rather than a word (Neo, question 562)
HIGH_STAKES_SENSE_CARVE_OUTS = (
    r"\b(?:input|output|cached?|cache[ -](?:read|write)|prompt|completion|re-?write|"
    r"total|context|thinking)\s+tokens?\b",
    r"\btokens?\s+(?:count|counts|budget|budgets|cost|costs|usage|economics|spend|"
    r"accounting|limit|limits|per\b|used\b)",
    r"\b\d+[km]?\s+tokens?\b",
)

# -- the tightened net (`validation.stakes_classifier: regex-tightened`)

#: THE TIGHTENED NET, and every row of it was derived from a MEASURED false positive (docs/superpowers/specs/2026-09-25-a-model-decides-what-is-high-stakes.md §6-)
_HST_DESTRUCTIVE_VERBS = (r"delet(?:e|es|ed|ing)|destroy(?:s|ed|ing)?|"
                          r"truncat(?:e|es|ed|ing)|drop(?:s|ped|ping)?|"
                          r"wip(?:e|es|ed|ing)")
#: 19 false positives, every one deleting CODE or a record about code
_HST_DESTRUCTIVE = (rf"\b(?:{_HST_DESTRUCTIVE_VERBS})\s+"
                    r"(?:(?:the|a|an|all|this|these|its|four|every|six)\s+)?"
                    + _NOT_A_PREPOSITION
                    + rf"(?:{_DATA_OBJECTS}|(?:live|production|prod)\s+\w+)\b"
                    r"|\birreversib|\bpurge")
#: A currency amount, or money named as money. 14 false positives spent a ROUND
_HST_MONEY_OBJECT = (r"(?:\$\s?[\d,]+(?:\.\d+)?|\b\d+(?:\.\d+)?\s*(?:usd|dollars?|"
                     r"cents?|eur|gbp)\b|\bdollars?\b|\busd\b|\bmoney\b|"
                     r"\breal\s+api\s+calls?\b)")
#: `bill` is DROPPED, not narrowed: 6 false positives and zero high rows
_HST_MONEY = (r"\bprice|\bpricing\b|\binvoice|\bcharge[ds]?\b"
              rf"|\bspen(?:d|ds|t|ding)\b[^.;]{{0,40}}?{_HST_MONEY_OBJECT}")
#: 4 false positives are the ADDED_COLUMNS mechanism or a gap left open on purpose ("no migration"
_HST_MIGRATION = (r"\b(?:run|runs|ran|running|appl(?:y|ies|ied|ying)|"
                  r"execut(?:e|es|ed|ing))\s+(?:(?:the|a|an|another|its|one)\s+)?"
                  r"(?:\w+\s+){0,2}(?:migrations?|backfill\w*)\b"
                  r"|\b(?:migrations?|backfill\w*)(?:\s+\w+){0,3}?\s+(?:was|were|is|are|"
                  r"be|been|gets?|got)\s+(?:\w+\s+){0,2}?(?:run|ran|applied|executed)\b"
                  + _HS_SCHEMA)
#: `make/makes/making a release` is 1 false positive ("what makes a release verifiable") and no high...
_HST_SHIPPING = (r"\bpublish\w*\s+(?:\S+\s+){0,3}?to\b"
                 r"|\bdeploy\w*\s+(?:\S+\s+){0,3}?to\b"
                 r"|\b(?:cut|cuts|cutting|ship|ships|shipped|shipping)\s+"
                 r"(?:a|the|another)\s+release\b" + _HS_SHIPPING_TAIL)
#: ADDITION 1 of 3, for 4 of the 6 rows the wide net MISSES: settling which version ships
_HST_VERSION = (r"\b(?:patch|minor|major)[\s-]?bump\w*"
                r"|\b(?:patch|minor|major)[\s-]?bumps?\s+the\b"
                r"|\bbump(?:s|ed|ing)?\s+(?:\S+\s+){0,3}?v?\d+\.\d+(?:\.\d+)?\b"
                r"|\b(?:release\s+)?version\s+v?\d+\.\d+\.\d+\b"
                r"|\b(?:tag|tags|tagged|tagging)\s+[\w./-]*\d+\.\d+\.\d+"
                r"|\brelease/[\w.-]*\d+\.\d+\.\d+")
#: ADDITION 2: moving a remote ref (`wo-29d99c67#3`, a force-push with a lease)
_HST_REMOTE_REF = (r"\bforce[- ]?push\w*|--force-with-lease|\bforce[- ]updat\w+"
                   r"|\b(?:updat|mov|repoint|push|reset)\w*\s+(?:only\s+)?"
                   r"(?:(?:the|a|its)\s+)?remote\s+(?:branch|ref|tag|head)\b")
#: ADDITION 3: arming or disarming a privileged-action control (`wo-551f5e8c#1`
_HST_GATE_CONTROL = (r"\brule-retract\b"
                     r"|\bretract\w*\s+(?:\S+\s+){0,3}?(?:rules?|exemptions?)\b"
                     r"|\b(?:live|learned|gate)\s+exemptions?\b"
                     r"|\bre-?arm(?:s|ed|ing)?\s+(?:(?:a|the)\s+)?gate")

HIGH_STAKES_TIGHTENED = (
    _HS_CREDENTIAL,
    _HS_PRODUCTION,     # deliberately untightened — see above
    _HST_DESTRUCTIVE,
    _HST_MIGRATION,
    _HST_MONEY,
    _HST_SHIPPING,
    _HS_PII,
    _HS_LEGAL,
    _HS_BREAKING,
    _HST_VERSION,
    _HST_REMOTE_REF,
    _HST_GATE_CONTROL,
)

#: The tightened net's extra senses
HIGH_STAKES_TIGHTENED_CARVE_OUTS = HIGH_STAKES_SENSE_CARVE_OUTS + (
    r"\b(?:single|zero|one|two|multi|per|no)[- ]token\b",
    r"\btoken[- ]efficien\w*",
    r"\bFORCE_\w*\s+tokens?\b",
    r"\b(?:json|valid|closing|closer|bare|last)\s+tokens?\b",
    r"\bon\s+delete\s+(?:cascade|set\s+null|restrict|no\s+action)\b",
    r"\bauth[\s-](?:failure|failures|error|errors|blocker|blockers|paused)\b",
    r"\bcould\s+not\s+authenticate\b",
    r"\blicen[cs]e\s+to\s+\w+",
)

_HIGH_STAKES_RE = re.compile("|".join(HIGH_STAKES), re.IGNORECASE)
_CARVE_OUT_RE = re.compile("|".join(HIGH_STAKES_SENSE_CARVE_OUTS), re.IGNORECASE)
_HIGH_STAKES_TIGHTENED_RE = re.compile("|".join(HIGH_STAKES_TIGHTENED), re.IGNORECASE)
_CARVE_OUT_TIGHTENED_RE = re.compile("|".join(HIGH_STAKES_TIGHTENED_CARVE_OUTS),
                                     re.IGNORECASE)


def high_stakes_marker(text: str, *, tightened: bool = False) -> str:
    """The high-stakes phrase this assumption contains, or `''`"""
    text = text or ""
    net = _HIGH_STAKES_TIGHTENED_RE if tightened else _HIGH_STAKES_RE
    carve = _CARVE_OUT_TIGHTENED_RE if tightened else _CARVE_OUT_RE
    carved = [m.span() for m in carve.finditer(text)]
    for m in net.finditer(text):
        start, end = m.span()
        if any(a <= start and end <= b for a, b in carved):
            continue
        return m.group(0)
    return ""


def tightened_verdict(text: str) -> Stakes:
    """The tightened net's answer in the CLASSIFIER's verdict type"""
    marker = high_stakes_marker(text, tightened=True)
    if not marker:
        return Stakes(high=False, category="none", reason="", model="regex-tightened")
    return Stakes(high=True, category="",
                  reason=f"its text says {marker!r}", model="regex-tightened")


# -- the second net's second gate: a diff the OS will not copy into a question

#: SECRET-SHAPED EVIDENCE, and it is deliberately NOT part of `HIGH_STAKES`
SECRET_EVIDENCE = (
    r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----",
    r"\bssh-(?:rsa|dss|ed25519)\s+AAAA[0-9A-Za-z+/=]+",
    # `["\']?` on BOTH sides of the colon and nowhere else: `"Authorization": "Bearer …"` is the header...
    r"\bAuthorization[\"\']?\s*:\s*[\"\']?(?:Bearer|Basic|Token)\s+\S+",
)

#: What each of `SECRET_EVIDENCE`'s shapes is CALLED on the record
SECRET_EVIDENCE_NAMES = (
    "a private key block",
    "an ssh key line",
    "an Authorization header value",
)

#: A VALUE THAT IS NOT A SECRET, however secret its name
SECRET_PLACEHOLDERS = (
    r"none|null|nil|nan|true|false|str|int|bool|x+|\.+|-+|_+",
    r"todo|tbd|fixme|changeme|change[-_]me|placeholder|redacted|dummy|fake|sample",
    r"example|examples|test|testing|secret|password|passwd|token|key|value",
    r"your[-_].*|my[-_].*|some[-_].*|the[-_].*",
)

#: PATHS ONLY SECRETS LIVE AT
SECRET_PATHS = (
    r"(?:^|/)\.env(?:\.[\w.-]+)?$",
    r"\.(?:pem|key|p12|pfx|jks|keystore)$",
    r"(?:^|/)id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?$",
    r"(?:^|/)credentials(?:\.[\w-]+)?$",
    r"(?:^|/)\.(?:netrc|npmrc|pypirc|pgpass)$",
    r"(?:^|/)secrets?\.[\w-]+$",
    r"service[-_]account[\w-]*\.json$",
)

#: The words that make an assignment's LEFT SIDE a secret's name
SECRET_NAME_PARTS = frozenset({
    "key", "keys", "apikey", "accesskey", "privatekey", "secretkey", "seckey",
    "token", "tokens", "authtoken", "secret", "secrets", "clientsecret",
    "password", "passwd", "passphrase", "pwd", "credential", "credentials",
})

#: One line assigning something
_ASSIGNMENT_RE = re.compile(
    r"^[ \t]*(?:(?:export|set|const|let|var|readonly)[ \t]+)?"
    r"(?P<quote>[\"\']?)(?P<name>[A-Za-z_][A-Za-z0-9_.-]*)(?P=quote)"
    r"[ \t]*(?:=>|:=|=|:)[ \t]*"
    r"(?P<value>[^\r\n]*?)[ \t]*[,;]?[ \t]*$")

#: The charset a credential is spelled in
_SECRET_VALUE_CHARS = re.compile(r"[A-Za-z0-9+/=._~-]+")

_SECRET_EVIDENCE_RE = [re.compile(p, re.IGNORECASE) for p in SECRET_EVIDENCE]
_SECRET_PATH_RE = [re.compile(p, re.IGNORECASE) for p in SECRET_PATHS]
_PLACEHOLDER_RE = re.compile("|".join(SECRET_PLACEHOLDERS), re.IGNORECASE)
_WORD_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")


def _names_a_secret(identifier: str) -> bool:
    parts = [p.lower() for p in _WORD_SPLIT_RE.split(identifier) if p]
    return any(p in SECRET_NAME_PARTS for p in parts)


def _secret_value(raw: str) -> bool:
    """Is this assignment's right side a REAL-LOOKING credential?"""
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    if len(value) < 6 or not _SECRET_VALUE_CHARS.fullmatch(value):
        return False
    if _PLACEHOLDER_RE.fullmatch(value):
        return False
    has_digit = any(c.isdigit() for c in value)
    has_alpha = any(c.isalpha() for c in value)
    # A digit beside letters, or sheer length
    return (has_digit and has_alpha) or len(value) >= 20


def _paths(stat: str, diff: str) -> list[str]:
    """Every path the stat and the diff HEADERS name"""
    found = [line.split("|")[0].strip() for line in (stat or "").splitlines()]
    for line in (diff or "").splitlines():
        if line.startswith(("--- ", "+++ ", "diff --git ", "rename to ", "copy to ")):
            for token in line.split()[1:]:
                found.append(re.sub(r"^[ab]/", "", token))
    return [p for p in found if p and p not in ("a", "b", "/dev/null")]


def secret_marker(stat: str, diff: str) -> str:
    """The secret-shaped thing this evidence carries, NAMED and never quoted (docs/superpowers/specs/2026-10-14-neo-never-reads-a-diff.md)"""
    for path in _paths(stat, diff):
        for pattern in _SECRET_PATH_RE:
            if pattern.search(path):
                return path[:200]
    for line in (diff or "").splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        marker = _line_marker(line[1:])
        if marker:
            return marker
    return ""


def secret_marker_text(text: str) -> str:
    """The same nets over PLAIN TEXT — the result summary"""
    for line in (text or "").splitlines():
        marker = _line_marker(line)
        if marker:
            return marker
    return ""


def _line_marker(line: str) -> str:
    """One line, no diff marker"""
    for pattern, name in zip(_SECRET_EVIDENCE_RE, SECRET_EVIDENCE_NAMES):
        if pattern.search(line):
            return name
    m = _ASSIGNMENT_RE.match(line)
    if m and _names_a_secret(m.group("name")) and _secret_value(m.group("value")):
        return f"a line assigning {m.group('name')[:60]}"
    return ""


@dataclass(frozen=True)
class Decision:
    """Armed to ask Neo, or held with a reason a person can read"""

    armed: bool
    code: str
    reason: str
    assumption_id: int = 0
    #: The assumption's position in its work order's list, as `all_assumptions` numbers it
    n: int = 0
    #: The escalated round this hold is about, 0 when no round was named
    round: int = 0


@dataclass(frozen=True)
class Ruling:
    """What Neo's reply MEANS for one assumption, after the code has had its say"""

    accept: bool
    reason: str
    stakes: str
    model: str
    #: True when Neo accepted and this module overrode it — the `stakes: high` net
    overridden: bool = False


def _held(code: str, reason: str, **fields: Any) -> Decision:
    return Decision(armed=False, code=code, reason=reason, **fields)


#: The panel reason, clamped to one line beside an assumption
_HINT_CHARS = 120


def _round_hint(round_reason: str) -> str:
    text = " ".join(str(round_reason or "").split())
    return text[:_HINT_CHARS - 3] + "…" if len(text) > _HINT_CHARS else text


def _panel_gave_up(round_n: int, round_reason: str, tail: str,
                   fields: dict[str, Any]) -> Decision:
    """The `panel_gave_up` hold, built once for all three decision functions (#778)"""
    on_round = f" on round {round_n}" if round_n else ""
    hint = _round_hint(round_reason)
    return _held(HELD_PANEL_GAVE_UP,
                 f"the validation panel gave up{on_round} and put this work order in "
                 f"front of you{f' — {hint}' if hint else ''} — {tail}",
                 round=round_n, **fields)


def decide_evidence(assumption: dict[str, Any], summary: str = "") -> Decision:
    """May the OS put THESE TEXTS into a stored question? (docs/superpowers/specs/2026-10-14-neo-never-reads-a-diff.md)"""
    aid = int(assumption.get("id") or 0)
    n = int(assumption.get("n") or 0)
    fields = {"assumption_id": aid, "n": n}
    marker = secret_marker_text(summary)
    if marker:
        # WHICH TEXT, named: the user has to know where to go and look
        return _held(HELD_EVIDENCE_SECRET,
                     f"the work order's result summary carries {marker}, and the OS "
                     f"will not copy a secret into a stored question — assumption #{n} "
                     f"is yours",
                     **fields)
    marker = secret_marker_text(str(assumption.get("content") or ""))
    if marker:
        return _held(HELD_EVIDENCE_SECRET,
                     f"the assumption's own text carries {marker}, and the OS will not "
                     f"copy a secret into a stored question — assumption #{n} is yours",
                     **fields)
    return Decision(armed=True, code="armed",
                    reason=f"the evidence for assumption #{n} carries nothing "
                           f"secret-shaped",
                    **fields)


def _stakes_hold(n: int, verdict: Stakes, fields: dict[str, Any]) -> Decision:
    """The high-stakes hold as the CLASSIFIER states it"""
    category = f" ({verdict.category})" if verdict.category else ""
    return _held(HELD_HIGH_STAKES,
                 f"assumption #{n} commits to an act{category}: "
                 f"{verdict.reason or 'no reason given'} — the OS does not decide those "
                 f"for you, whatever it thinks of them", **fields)


def decide(assumption: dict[str, Any], wo: dict[str, Any], cfg: Any, *,
           round_outcome: str = "", round_n: int = 0, round_reason: str = "",
           refusal_answered: bool = True, undeclared_delivery: bool = False,
           asked_question_id: int = 0,
           unreachable_question_ids: Collection[int] = (),
           stakes: Stakes | None = None) -> Decision:
    """May the OS decide this assumption right now?"""
    if not (getattr(cfg, "enabled", False) and getattr(cfg, "auto_review", False)):
        return _held(HELD_DISABLED,
                     "this project has not given the OS permission to decide its "
                     "assumptions (`validation.auto_review`)")
    status = str(wo.get("status") or "")
    if status != REVIEW_PASS_STATUS:
        return _held(HELD_STATUS,
                     f"the work order is {status or 'in no status'}, not waiting on a "
                     f"review")

    aid = int(assumption.get("id") or 0)
    n = int(assumption.get("n") or 0)
    fields = {"assumption_id": aid, "n": n}
    if str(assumption.get("status") or "") != "pending":
        return _held(HELD_SETTLED,
                     f"assumption #{n} is already {assumption.get('status')}", **fields)
    if str(round_outcome or "").lower() == "escalated":
        return _panel_gave_up(round_n, round_reason,
                              "settling its assumptions would answer that for you too",
                              fields)
    if not refusal_answered:
        return _held(HELD_REFUSAL_UNANSWERED,
                     REFUSAL_UNDECLARED_REASON if undeclared_delivery
                     else REFUSAL_UNANSWERED_REASON, **fields)
    asked = int(assumption.get("neo_question_id") or 0)
    if asked and asked != int(asked_question_id or 0) \
            and asked not in unreachable_question_ids:
        return _held(HELD_ASKED,
                     f"assumption #{n} is already with Neo (question {asked})", **fields)
    if stakes is not None:
        # The caller computed the verdict (`Daemon._classify_stakes`) and this function stays PURE
        if stakes.high:
            return _stakes_hold(n, stakes, fields)
    else:
        marker = high_stakes_marker(str(assumption.get("content") or ""))
        if marker:
            return _held(HELD_HIGH_STAKES,
                         f"assumption #{n} mentions {marker!r} — the OS does not decide "
                         f"those for you, whatever it thinks of them", **fields)
    return Decision(armed=True, code="armed",
                    reason=f"assumption #{n} is routine enough to put to Neo", **fields)


def decide_confirm(assumption: dict[str, Any], wo: dict[str, Any], cfg: Any, *,
                   round_outcome: str = "", round_n: int = 0, round_reason: str = "",
                   refusal_answered: bool = True,
                   objections_outstanding: bool = False,
                   unreachable_question_ids: Collection[int] = (),
                   confirmation_open: bool = True,
                   stakes: Stakes | None = None) -> Decision:
    """May the OS CONFIRM this early verdict now, at delivery? (docs/superpowers/specs/2026-09-23-an-assumption-judged-while-the-worker-still-runs.md §7.)"""
    verdict = str(assumption.get("provisional_verdict") or "")
    aid = int(assumption.get("id") or 0)
    n = int(assumption.get("n") or 0)
    fields = {"assumption_id": aid, "n": n}
    # 2026-10-01-a-confirmation-is-not-re-run-on-a-settled-assumption.md §3.2
    if str(assumption.get("status") or "") != "pending":
        return _held(HELD_SETTLED,
                     f"assumption #{n} is already {assumption.get('status')}", **fields)
    if not verdict:
        return _held(HELD_UNJUDGED,
                     f"assumption #{n} carries no early verdict — there is nothing to "
                     f"confirm", **fields)
    if verdict != PROVISIONAL_ACCEPT:
        return _held(HELD_OBJECTED,
                     f"Neo objected to assumption #{n} while the work ran — it approved "
                     f"nothing, so there is nothing to confirm and it is yours",
                     **fields)
    confirming = int(assumption.get("confirm_question_id") or 0)
    if confirming and confirming not in unreachable_question_ids:
        if not confirmation_open:
            # 2026-09-28 spec §6: the question id is in the PROSE because the dashboard's link is driven by...
            return _held(HELD_CONFIRM_SPENT,
                         f"assumption #{n} is yours — the OS asked Neo to confirm its "
                         f"early reading (question {confirming}) and that question is no "
                         f"longer open", **fields)
        return _held(HELD_CONFIRMING,
                     f"assumption #{n} is already with Neo to confirm "
                     f"(question {confirming})", **fields)
    if round_n > 0 and str(round_outcome or "").lower() in _UNRESOLVED_OUTCOMES:
        # 2026-09-28 spec §5.1
        return _held(HELD_ROUND_OPEN,
                     f"the validation panel has not finished round {round_n} — the "
                     f"result this confirms against may be about to be sent back",
                     round=round_n, **fields)
    if objections_outstanding:
        return _held(HELD_OBJECTION_IN_FLIGHT,
                     "an objection on this work order has not reached the worker or "
                     "been withdrawn yet — confirming is retried once it has", **fields)
    return decide(assumption, wo, cfg, round_outcome=round_outcome,
                  # The round travels with its outcome: the hold it produces names which round gave up and quotes what...
                  round_n=round_n, round_reason=round_reason,
                  refusal_answered=refusal_answered,
                  asked_question_id=int(assumption.get("neo_question_id") or 0),
                  # Forwarded, not consumed: both gates need it
                  unreachable_question_ids=unreachable_question_ids,
                  # Forwarded unchanged (2026-09-25 spec SS3.7): the confirmation pass is gated by the same verdict the...
                  stakes=stakes)


def decide_early(assumption: dict[str, Any], wo: dict[str, Any], cfg: Any, *,
                 round_outcome: str = "", round_n: int = 0, round_reason: str = "",
                 refusal_answered: bool = True, undeclared_delivery: bool = False,
                 asked_question_id: int | None = None,
                 unreachable_question_ids: Collection[int] = (),
                 stakes: Stakes | None = None) -> Decision:
    """May the OS put this assumption to Neo WHILE THE WORKER IS STILL TYPING? (docs/superpowers/specs/2026-09-23-an-assumption-judged-while-the-worker-still-runs.md §5.1.)"""
    if not (getattr(cfg, "enabled", False) and getattr(cfg, "auto_review", False)):
        return _held(HELD_DISABLED,
                     "this project has not given the OS permission to decide its "
                     "assumptions (`validation.auto_review`)")
    status = str(wo.get("status") or "")
    if status != EARLY_PASS_STATUS:
        return _held(HELD_STATUS,
                     f"the work order is {status or 'in no status'}, not running — there "
                     f"is no worker to tell")

    aid = int(assumption.get("id") or 0)
    n = int(assumption.get("n") or 0)
    fields = {"assumption_id": aid, "n": n}
    if str(assumption.get("status") or "") != "pending":
        return _held(HELD_SETTLED,
                     f"assumption #{n} is already {assumption.get('status')}", **fields)
    if str(assumption.get("provisional_verdict") or ""):
        return _held(HELD_JUDGED,
                     f"assumption #{n} already carries an early verdict "
                     f"({assumption.get('provisional_verdict')})", **fields)
    if str(round_outcome or "").lower() == "escalated":
        return _panel_gave_up(round_n, round_reason,
                              "the OS does not rule on its assumptions while it waits "
                              "for you", fields)
    if not refusal_answered:
        return _held(HELD_REFUSAL_UNANSWERED,
                     REFUSAL_UNDECLARED_REASON if undeclared_delivery
                     else REFUSAL_UNANSWERED_REASON, **fields)
    asked = int(assumption.get("neo_question_id") or 0)
    if asked and asked != int(asked_question_id or 0) \
            and asked not in unreachable_question_ids:
        return _held(HELD_ASKED,
                     f"assumption #{n} is already with Neo (question {asked})", **fields)
    if stakes is not None:
        # The caller computed the verdict (`Daemon._classify_stakes`) and this function stays PURE
        if stakes.high:
            return _stakes_hold(n, stakes, fields)
    else:
        marker = high_stakes_marker(str(assumption.get("content") or ""))
        if marker:
            return _held(HELD_HIGH_STAKES,
                         f"assumption #{n} mentions {marker!r} — the OS does not decide "
                         f"those for you, whatever it thinks of them", **fields)
    return Decision(armed=True, code="armed",
                    reason=f"assumption #{n} is routine enough to put to Neo while the "
                           f"worker can still act on it", **fields)


def read_ruling(verdict: dict[str, Any], default_model: str = "") -> Ruling:
    """What Neo's reply means for one assumption"""
    stakes = str(verdict.get("stakes") or "").strip().lower() or STAKES_UNCLASSIFIED
    reason = str(verdict.get("reason") or "").strip() or "no reason given"
    model = str(verdict.get("model") or default_model or "")
    accept = (not verdict.get("escalate")) and verdict.get("verdict") == "approved"
    if accept and stakes not in ROUTINE_STAKES:
        # Two spellings of one override, because the user reads this line and the two facts are different:...
        said = (f"marked it high-stakes, and those are yours"
                if stakes == STAKES_HIGH else
                "did not classify the stakes at all, and the OS does not read silence "
                "as routine"
                if stakes == STAKES_UNCLASSIFIED else
                f"classified the stakes as {stakes!r}, which the OS cannot read as "
                f"routine")
        return Ruling(accept=False, stakes=stakes, model=model, overridden=True,
                      reason=f"Neo would have accepted it but {said}: {reason}")
    if not accept and not verdict.get("escalate"):
        return Ruling(accept=False, stakes=stakes, model=model,
                      reason=f"Neo would have turned this down: {reason}")
    return Ruling(accept=accept, stakes=stakes, model=model, reason=reason)


def escalation_cause(ruling: Ruling | None = None, *, escalate: bool = False,
                     over_cap: bool = False) -> str:
    """WHY THE OS ESCALATED A QUESTION NEO HAD ALREADY ANSWERED, as one groupable label (docs/superpowers/specs/2026-10-01-neo-observability.md)"""
    if over_cap:
        return "" if escalate else "scope-over-cap"
    if ruling is None or escalate:
        return ""
    if ruling.overridden:
        if ruling.stakes == STAKES_HIGH:
            return "stakes-high"
        if ruling.stakes == STAKES_UNCLASSIFIED:
            return "stakes-unclassified"
        # `read_ruling`'s third `said` branch: a word that is neither, and not routine
        return "" if ruling.stakes in ROUTINE_STAKES else "stakes-unreadable"
    # `verdict: deny` with no machine rejection behind it (module docstring)
    return "" if ruling.accept else "neo-denied"


# -- the reviewer


ASSUMPTION_REVIEWER_PERSONA = """You are Neo, the user's delegate inside the Jarvis \
agentic OS, ruling on ONE assumption a worker recorded.

An assumption is a worker saying: "you did not specify this, I had to decide it, and here
is what I chose." Until now every one of them waited for the user. Your job is to take the
ROUTINE ones off their desk and leave the rest exactly where they are.

YOUR ANSWERS, and which are open to you depends on what the question tells you about
the work order:
- ACCEPT — `{"escalate": false, "verdict": "approve", "stakes": "routine", "reason": "…"}`.
  The worker's call is one the user would have made, or one that costs nothing either way.
- ESCALATE — `{"escalate": true, "verdict": "deny", "stakes": "…", "reason": "…"}`. The
  user decides this one.
- OBJECT — `{"escalate": false, "verdict": "deny", "stakes": "routine", "reason": "…"}`,
  and **only when the question says the work order is STILL RUNNING.** Your `reason` is
  then delivered to that worker as guidance while it is mid-task.

YOU CANNOT REJECT AN ASSUMPTION AND OBJECTING IS NOT REJECTING. Nothing you answer
settles anything by itself: an acceptance on a running order is PROVISIONAL and you will
be asked again once the work exists, and an objection leaves the assumption exactly where
it was — pending, and the user's to decide. What an objection buys is the one thing a
ruling on an intention is good for: the worker finds out now instead of building on it for
the rest of its turn. On a work order that has FINISHED there is no worker to tell, so
your two answers there are accept and escalate, and a disagreement is an escalation
carrying your reading.

ACCEPT when the assumption is mechanical or conventional: naming, file layout, test
placement, comment and docstring style, branch and commit shape, which of two equivalent
libraries already in the project was used, an internal helper's signature, log wording.
These are decisions in name only, and charging the user a review action for one is the
cost this exists to remove.

JUDGE IT AGAINST THE ASK (ponytail: the smallest change that fully solves it). Choosing the
smaller option that still meets the ask is routine: accept it, and never object that the
worker did not add a feature, option, abstraction, refactor, test or doc the ask does not
need. An assumption that ADDS such scope, or drops part of the ask, is the opposite case.

ESCALATE when the assumption CHANGES SOMETHING THE USER WOULD RECOGNISE: user-visible
behaviour or wording, a default value, an API or CLI surface others call, a data shape
written to disk, an error the user will see, scope the worker added or dropped, a
dependency added, or anything the work order's own text said the user cared about. Also
escalate whenever you would need to know a preference you have no learning about — a
learning below is authority, and its absence is not.

`stakes` IS A SEPARATE JUDGEMENT FROM YOUR VERDICT, and you must give it on every answer.
Say `"high"` when the assumption touches production or live credentials, spends money,
deletes or publishes anything, carries legal or personal-data weight, or would be painful
to undo — even if you are also accepting it. High stakes goes to the user whatever you
ruled, and marking one honestly is how you stay trusted on the rest.

THE DECISION RECORD IS AUTHORITY AND YOU MAY CITE IT. What it lists has already been
decided on this work order — a question the user or Neo answered, a message the user sent,
an assumption they already ruled on. When it settles the assumption in front of you,
ACCEPT and name the id in your `reason`; escalating to ask for a decision that is quoted
in your own prompt spends a review action the user has already spent. An item marked
`(withheld …)`, `(not answered)` or `(not shown)` is NOT a ruling, and neither is an
omission line: those are cases to escalate, not blanks to fill in.

A SIBLING MARKED `(withheld — high-stakes …)` IS NOT A BLANK TO FILL IN. The OS holds
those assumptions for the user and does not show you their text, deliberately. Do not
guess at what one says, and do not treat its absence as permission: if your ruling would
turn on what it contains, that is precisely a case to ESCALATE.

WHEN IN ANY DOUBT, ESCALATE. The cost of escalating wrongly is one review action the user
was going to make anyway. The cost of accepting wrongly is a decision they never made,
shipped in their name.

RULING ON A RUNNING ORDER MEANS RULING ON AN INTENTION. There is no diff and no result
summary, because the work does not exist yet; the question says so where the summary would
be. Judge what the worker says it decided, and if your ruling would turn on a result you
cannot see, escalate rather than guess.

`reason` is ONE LINE, and WHO READS IT DEPENDS ON YOUR ANSWER. On an acceptance it says
why the call was routine and the user reads it on the record. On an escalation it says
what the user has to decide. **On an objection the WORKER reads it, mid-task** — so write
it to that reader: say what is wrong with the call and what to do instead, in the
imperative, with no preamble and nothing about the machinery that sent it.

An escalation may carry one optional label, which is grouped and reported:
  "cause": "<on an escalation, name the cause from this list; omit it if none fits: \
high-stakes | no-learning-applies | evidence-insufficient>"
"""


def sibling_line(s: dict[str, Any], *, cited: bool = False) -> str:
    """One sibling assumption as CONTEXT, with the high-stakes net applied to it too"""
    content = str(s.get("content") or "")
    marker = " (cited by the text)" if cited else ""
    if high_stakes_marker(content):
        return (f"  #{s['n']}{marker} [{s['status']}] (withheld — high-stakes, and the "
                f"user's alone to decide)")
    return f"  #{s['n']}{marker} [{s['status']}] {content[:200]}"


def _cited_sibling_item(s: dict[str, Any]) -> str:
    """One CITED sibling: its text and its ruling, netted exactly as an uncited one (spec §4a)"""
    item = _netted(f"#{s.get('n')} (cited by the text)",
                   sibling_line(s, cited=True), (str(s.get("content") or ""),))
    ruling = _user_ruling_item(s)
    return f"{item}\n{ruling}" if ruling else item


def decision_record(store: Any, neo: Any, wo_id: str, siblings: list[dict[str, Any]],
                    assumption: dict[str, Any] | None = None, *,
                    chars: int = DEFAULT_VALIDATION_DECISION_RECORD_CHARS) -> str:
    """What has ALREADY been decided on this work order, newest first (#832)"""
    # One row past the bound is how the overflow becomes VISIBLE rather than silent (§3)
    answered = list(neo.answered_questions(wo_id, limit=_ANSWERED_ROWS + 1) or [])
    cut_by_rows = max(0, len(answered) - _ANSWERED_ROWS)
    answered = answered[:_ANSWERED_ROWS]
    n_by_question = {}
    for s in siblings:
        for column in ("neo_question_id", "confirm_question_id"):
            if s.get(column):
                n_by_question[int(s[column])] = s.get("n")

    content = str((assumption or {}).get("content") or "")
    cited_ids = cited_question_ids(content)
    cited_ns = set(cited_sibling_ns(content))
    by_id = {int(q["id"]): q for q in answered}
    # Cited FIRST and exempt from the cap: it is the single item most likely to be why this packet exists...
    head = cited_lines(neo, wo_id, content, siblings, by_id=by_id,
                       n_by_question=n_by_question)

    items = [_question_item(q, n_by_question) for q in answered
             if int(q["id"]) not in cited_ids]
    items += [_message_item(m) for m in (store.user_messages(wo_id) or [])]
    items += [line for s in siblings
              if s.get("n") not in cited_ns and (line := _user_ruling_item(s))]

    kept, dropped, spent = [], 0, 0
    for item in items:
        if spent + len(item) + 1 > chars:
            dropped = len(items) - len(kept)
            break
        kept.append(item)
        spent += len(item) + 1
    lines = [*head, *kept]
    if not lines:
        return ""
    if dropped or cut_by_rows:
        # An omission stated, and LAST (kn-1485b845)
        why = f"the record is capped at {chars} characters"
        if cut_by_rows:
            why += f" and {_ANSWERED_ROWS} answered questions"
        omitted = dropped + cut_by_rows
        lines.append(f"  (… {'at least ' if cut_by_rows else ''}{omitted} older "
                     f"item{'' if omitted == 1 else 's'} omitted — {why})")
    return "\n".join(lines)


def cited_question_ids(text: str) -> list[int]:
    """Question ids this text names, deduplicated, capped"""
    found: list[int] = []
    for match in _CITED_QUESTION_RE.finditer(text or ""):
        qid = int(match.group(1))
        if qid not in found:
            found.append(qid)
    return found[:_CITED_LIMIT]


def cited_sibling_ns(text: str) -> list[int]:
    """Sibling assumption numbers this text names: `assumption #2`, `assumptions 2 and 3` (spec §4a)"""
    found: list[int] = []
    for match in _CITED_SIBLING_RE.finditer(text or ""):
        at = match.end()
        while (tail := _CITED_SIBLING_TAIL_RE.match(text, at)) is not None:
            n = int(tail.group(1))
            if n not in found:
                found.append(n)
            at = tail.end()
    return found[:_CITED_LIMIT]


def cited_ref_problem(neo: Any, wo_id: str, text: str,
                      siblings: list[dict[str, Any]]) -> str:
    """Why an in-text reference cannot resolve, or `''` (spec §4c).

    A question asked on THIS order and not yet answered is NOT a problem: it has a
    rendering of its own. Sibling `n` is order-scoped, so "foreign" is a question-id case.
    """
    for qid in cited_question_ids(text):
        row = neo.get(qid)
        if row is None:
            return f"question {qid} is cited, but there is no such question"
        if str(row.get("wo_id") or "") != wo_id:
            return (f"question {qid} is cited, but it belongs to another work order — "
                    f"cite a question asked on this one")
    numbers = {int(s["n"]) for s in siblings if s.get("n") is not None}
    for n in cited_sibling_ns(text):
        if n not in numbers:
            return f"this order has no assumption #{n}"
    return ""


def cited_lines(neo: Any, wo_id: str, text: str, siblings: list[dict[str, Any]], *,
                by_id: dict[int, dict[str, Any]] | None = None,
                n_by_question: dict[int, Any] | None = None) -> list[str]:
    """Every in-text reference in `text`, resolved and rendered — ONE resolver, both verbs"""
    answered = by_id or {}
    by_n = {int(s["n"]): s for s in siblings if s.get("n") is not None}
    lines = [_cited_item(neo, wo_id, qid, answered.get(qid), n_by_question or {})
             for qid in cited_question_ids(text)]
    lines += [_cited_sibling_item(by_n[n]) for n in cited_sibling_ns(text)
              if n in by_n]
    return lines


def cited_context(neo: Any, wo_id: str, text: str, siblings: list[dict[str, Any]], *,
                  cfg: Any = None) -> str:
    """The `jarvis wo ask` half of §4b: the same refs, bounded by `decision_record_chars`"""
    chars = _record_chars(cfg)
    kept, spent, dropped = [], 0, 0
    for item in cited_lines(neo, wo_id, text, siblings):
        if spent + len(item) + 1 > chars:
            dropped += 1
            continue
        kept.append(item)
        spent += len(item) + 1
    if dropped:
        # An omission stated, and LAST (kn-1485b845)
        kept.append(f"  (… {dropped} cited item{'' if dropped == 1 else 's'} omitted — "
                    f"the record is capped at {chars} characters)")
    return "\n".join(kept)


def _cited_item(neo: Any, wo_id: str, qid: int, answered: dict[str, Any] | None,
                n_by_question: dict[int, Any]) -> str:
    """One cited id, resolved (#832)"""
    cited = "(cited by the assumption"
    if answered is not None:
        return _question_item(answered, n_by_question, cited=True, truncate=False)
    row = neo.get(qid)
    if row is None:
        return f"  Q{qid} {cited}; no such question)"
    if str(row.get("wo_id") or "") != wo_id:
        return f"  Q{qid} {cited}; belongs to another work order — not shown)"
    if str(row.get("status") or "") == "answered":
        return _question_item(row, n_by_question, cited=True, truncate=False)
    return f"  Q{qid} {cited}; asked on this order, not answered)"


def _question_item(q: dict[str, Any], n_by_question: dict[int, Any], *,
                   cited: bool = False, truncate: bool = True) -> str:
    """One answered question"""
    qid = int(q["id"])
    if str(q.get("kind") or "") == QUESTION_KIND:
        n = n_by_question.get(qid)
        headline = raw_headline = (f"assumption #{n}" if n is not None
                                   else "assumption (row not found)")
    else:
        raw_headline = str(q.get("question") or "")
        first_line = raw_headline.strip().splitlines()
        headline = (first_line[0] if first_line else "(no question text)")[:160]
    raw_answer = str(q.get("answer") or "(no answer recorded)")
    answer = " ".join(raw_answer.split())
    if truncate:
        answer = answer[:800]
    tail, raw_tail = "", ""
    if str(q.get("review_status") or "") == "corrected":
        # `neo_store.review` leaves the user's correction as the only ruling that survived
        raw_tail = str(q.get("review_feedback") or "")
        tail = f" (the user corrected this: {' '.join(raw_tail.split())[:200]})"
    marker = " (cited by the assumption)" if cited else ""
    return _netted(
        f"Q{qid}{marker}",
        f"  Q{qid}{marker} [answered by {q.get('answered_by') or 'unknown'}] "
        f"{headline} -> {answer}{tail}",
        (raw_headline, raw_answer, raw_tail))


def _message_item(m: dict[str, Any]) -> str:
    raw = str(m.get("body") or "")
    stamp = _stamp(m.get("ts"))
    return _netted(f"[user message {stamp}]",
                   f"  [user message {stamp}] {' '.join(raw.split())[:400]}", (raw,))


def _user_ruling_item(s: dict[str, Any]) -> str:
    """A sibling assumption the USER settled, with the reasoning they gave, or `''`"""
    status = str(s.get("status") or "")
    if status in ("", "pending") or str(s.get("decided_by") or "") == DECIDER:
        return ""
    raw = str(s.get("decided_reason") or "")
    reason = " ".join(raw.split())[:400]
    return _netted(f"#{s.get('n')} {status}",
                   f"  #{s.get('n')} {status} by the user"
                   f"{' — ' + reason if reason else ''}", (raw,))


def _netted(label: str, rendered: str, payload: tuple[str, ...]) -> str:
    """The secret net over ONE item (#832)"""
    marker = secret_marker_text("\n".join(payload))
    return rendered if not marker else f"  {label} (withheld — carries {marker})"


def _stamp(ts: Any) -> str:
    """A record timestamp as a date, for ordering the reviewer can read"""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return "unknown time"


#: The decision record's heading, and its empty case
RECORD_HEADING = "# What has already been decided on this work order — authority you may CITE"
NO_RECORD = "  (no prior decisions recorded)"


def _record_block(record: str) -> str:
    return f"{RECORD_HEADING}\n{record or NO_RECORD}"


#: The spec block's heading and its two empty cases (#1035)
SPEC_HEADING = "# THE SPEC SECTION THIS WORK ORDER WAS BUILT TO — authority you may CITE"
NO_SPEC = "  (no spec section — this work order was not planned against one)"
SPEC_INTRO = ("This section is what the work was supposed to be. Where it settles the "
              "assumption, rule on it and cite the section.")


def _spec_block(spec: dict[str, str] | None, chars: int) -> str:
    """The section `specs.spec_of` resolved, bounded — or that there is none, in words (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md §2)"""
    if not spec:
        return f"{SPEC_HEADING}\n{NO_SPEC}"
    ref = (f"{spec.get('repo_path') or '(no path recorded)'} § "
           f"{spec.get('section') or '(no section named)'}")
    text = (spec.get("section_text") or "").strip()
    if not text:
        return (f"{SPEC_HEADING} — {ref}\n  (no spec section in the packet: the named "
                f"section did not resolve in that document)")
    return f"{SPEC_HEADING} — {ref}\n{SPEC_INTRO}\n{_bounded(text, chars, ref)}"


def _bounded(text: str, chars: int, ref: str) -> str:
    """`chars` of the section, with any cut ANNOUNCED IN WORDS and the reference repeated so the reviewer..."""
    body = text[:chars]
    if len(text) > chars:
        body += (f"\n[spec section truncated — {len(body):,} of {len(text):,} chars; "
                 f"full section: {ref}]")
    return body


#: The SECOND spec block: the sections this ASSUMPTION cites (#102)
CITED_HEADING = "# THE SPEC SECTION THIS ASSUMPTION CITES — authority you may CITE"
CITED_INTRO = ("The assumption above argues from this section. Where it settles the "
               "assumption, rule on it and cite the section.")


def _cited_ref(assumption: dict[str, Any]) -> str:
    """Every reference the pointer columns name, read off them pairwise"""
    paths = (assumption.get("spec_path") or "").splitlines()
    whichs = (assumption.get("spec_which") or "").splitlines()
    refs = [f"{p or '(no path recorded)'} § {w or '(no section named)'}"
            for p, w in zip_longest(paths, whichs, fillvalue="")]
    ref = "; ".join(refs) or "(no path recorded) § (no section named)"
    if len(paths) != len(whichs):
        ref += (f" [{len(paths)} paths recorded against {len(whichs)} section names — "
                f"the pairing above may be wrong]")
    return ref


def _cited_block(assumption: dict[str, Any], chars: int) -> str:
    """The sections the assumption cites, as `ops.assume` labelled them"""
    text = (assumption.get("spec_text") or "").strip()
    ref = _cited_ref(assumption)
    return f"{CITED_HEADING} — {ref}\n{CITED_INTRO}\n{_bounded(text, chars, ref)}"


def _spec_blocks(assumption: dict[str, Any], spec: dict[str, str] | None,
                 chars: int) -> list[str]:
    """The work order's spec block, then the sections the assumption cites if it cites any"""
    cited = (assumption.get("spec_text") or "").strip()
    built_to = ((spec or {}).get("section_text") or "").strip()
    if not cited:
        return [_spec_block(spec, chars)]
    if not built_to:
        return [_spec_block(spec, chars), _cited_block(assumption, chars)]
    a, b = _share(len(built_to), len(cited), chars)
    return [_spec_block(spec, a), _cited_block(assumption, b)]


def _share(a: int, b: int, chars: int) -> tuple[int, int]:
    """What each of two blocks of size `a` and `b` may spend of one `chars` budget"""
    floor = chars // 2
    if a + b <= chars:
        return a, b
    if a <= floor:
        return a, chars - a
    if b <= floor:
        return chars - b, b
    return floor, floor


def _ruling_question(project: str, wo: dict[str, Any], assumption: dict[str, Any],
                     siblings: list[dict[str, Any]], *, early: bool = False,
                     record: str = "", spec: dict[str, str] | None = None,
                     spec_chars: int = DEFAULT_VALIDATION_ASSUMPTION_SPEC_CHARS) -> str:
    """What the reviewer reads (#832)"""
    n = assumption.get("n")
    others = "\n".join(sibling_line(s) for s in siblings
                       if s["id"] != assumption["id"]) or "  (none)"
    # THE REVIEWER IS TOLD WHICH PASS THIS IS: it decides which answers are open to it...
    delivered = (
        "# THE WORK ORDER IS STILL RUNNING\nThe worker is mid-task: there is no diff and "
        "no result summary yet, and you are ruling on an intention. Your `reason` reaches "
        "that worker if you object, and an acceptance here is provisional — you will be "
        "asked again once the work exists."
        if early else
        f"# What the worker says it delivered\n"
        f"{(wo.get('result_summary') or '(nothing recorded)')[:1500]}")
    answers = (
        "Answer with `escalate`, `verdict` (`approve` to accept it, `deny` to OBJECT — "
        "your `reason` goes to the worker), `stakes` (`routine` or `high`) and a one-line "
        "`reason`. Set `escalate` true to leave it to the user instead."
        if early else
        "Answer with `escalate`, `verdict` (`approve` to accept it, `deny` to send it to "
        "the user), `stakes` (`routine` or `high`) and a one-line `reason`.")
    answers += (" When the decision record above settles it, say which id in `reason` "
                "instead of escalating to ask for it again; when the spec section does, "
                "cite the section.")
    return "\n\n".join([
        f"ASSUMPTION REVIEW — rule on assumption #{n} of {wo['id']} in {project}, and on "
        f"nothing else.",
        f"# The assumption\n{assumption.get('content') or '(empty)'}",
        f"# The work order it was recorded against\n{wo.get('title') or '(untitled)'}\n"
        f"{(wo.get('description') or '')[:2000]}",
        *_spec_blocks(assumption, spec, spec_chars),
        delivered,
        f"# The work order's other assumptions, for context only — do not rule on these\n"
        f"{others}",
        _record_block(record),
        answers,
    ])


#: The per-field cuts this question makes, named instead of spelled inline
CONFIRM_DESCRIPTION_CHARS = 2000
CONFIRM_SUMMARY_CHARS = 1500


def _confirm_question(project: str, wo: dict[str, Any], assumption: dict[str, Any],
                      siblings: list[dict[str, Any]],
                      record: str = "", spec: dict[str, str] | None = None,
                      spec_chars: int = DEFAULT_VALIDATION_ASSUMPTION_SPEC_CHARS) -> str:
    """What the reviewer reads at DELIVERY (#102)"""
    n = assumption.get("n")
    others = "\n".join(sibling_line(s) for s in siblings
                       if s["id"] != assumption["id"]) or "  (none)"
    return "\n\n".join([
        f"ASSUMPTION REVIEW — CONFIRM an earlier reading of assumption #{n} of "
        f"{wo['id']} in {project}, against the result that has now been delivered, and "
        f"rule on nothing else.",
        f"# The assumption\n{assumption.get('content') or '(empty)'}",
        f"# The reading formed WHILE THE WORKER WAS STILL TYPING\n"
        f"verdict: {assumption.get('provisional_verdict') or '(none)'} "
        f"(model: {assumption.get('provisional_model') or 'unknown'})\n"
        f"{assumption.get('provisional_reason') or '(no reason recorded)'}\n"
        f"That reading had no result summary in front of it. You do. NEITHER reading "
        f"sees the diff: you are confirming a reading against what the worker says it "
        f"DELIVERED, and whether the code matches this sentence is the validation "
        f"panel's judgement, not yours.",
        f"# The work order it was recorded against\n{wo.get('title') or '(untitled)'}\n"
        f"{(wo.get('description') or '')[:CONFIRM_DESCRIPTION_CHARS]}",
        *_spec_blocks(assumption, spec, spec_chars),
        f"# What the worker says it delivered\n"
        f"{(wo.get('result_summary') or '(nothing recorded)')[:CONFIRM_SUMMARY_CHARS]}",
        f"# The work order's other assumptions, for context only — do not rule on these\n"
        f"{others}",
        _record_block(record),
        "You are CONFIRMING that earlier reading against the delivered result. "
        "Confirming settles this assumption in the user's name; anything else leaves it "
        "with them, carrying both readings.",
        "Answer with `escalate`, `verdict` (`approve` to confirm it, `deny` to send it "
        "to the user), `stakes` (`routine` or `high`) and a one-line `reason`. When the "
        "decision record above settles it, say which id in `reason` instead of "
        "escalating to ask for it again; when the spec section does, cite the section.",
    ])


def _record_chars(cfg: Any) -> int:
    """This project's `validation.decision_record_chars`, or the fleet default with no `cfg`"""
    return int(getattr(cfg, "decision_record_chars",
                       DEFAULT_VALIDATION_DECISION_RECORD_CHARS))


def _spec_chars(cfg: Any) -> int:
    """This project's `validation.assumption_spec_chars`, or the fleet default with no `cfg`"""
    return int(getattr(cfg, "assumption_spec_chars",
                       DEFAULT_VALIDATION_ASSUMPTION_SPEC_CHARS))


def propose_confirmation(store: Any, neo: Any, project: str, wo: dict[str, Any],
                         assumption: dict[str, Any], siblings: list[dict[str, Any]],
                         *, cfg: Any = None) -> dict[str, Any]:
    """Put ONE already-judged assumption back to Neo at delivery"""
    question = neo.ask(project, wo["id"],
                       _confirm_question(project, wo, assumption, siblings,
                                         decision_record(
                                             store, neo, wo["id"], siblings, assumption,
                                             chars=_record_chars(cfg)),
                                         # GitHub issue #1035
                                         spec=specs.spec_of(store, wo),
                                         spec_chars=_spec_chars(cfg)),
                       context=f"{wo.get('title') or ''}\n"
                               f"{(wo.get('description') or '')[:800]}",
                       kind=QUESTION_KIND)
    store.link_assumption_confirmation(assumption["id"], question["id"])
    store.add_event(wo["id"], "autoreview_asked", {
        "assumption_id": assumption["id"], "n": assumption.get("n"),
        "neo_question_id": question["id"], "confirm": True})
    log.info("auto-review asked Neo to confirm assumption #%s of %s as question %s",
             assumption.get("n"), wo["id"], question["id"])
    return question


def propose(store: Any, neo: Any, project: str, wo: dict[str, Any],
            assumption: dict[str, Any], siblings: list[dict[str, Any]], *,
            early: bool = False, cfg: Any = None) -> dict[str, Any]:
    """Put ONE assumption to Neo"""
    question = neo.ask(project, wo["id"],
                       _ruling_question(
                           project, wo, assumption, siblings, early=early,
                           record=decision_record(store, neo, wo["id"], siblings,
                                                  assumption,
                                                  chars=_record_chars(cfg)),
                           # GitHub issue #1035: resolved here, so no caller changes
                           spec=specs.spec_of(store, wo),
                           spec_chars=_spec_chars(cfg)),
                       context=f"{wo.get('title') or ''}\n"
                               f"{(wo.get('description') or '')[:800]}",
                       kind=QUESTION_KIND)
    store.link_assumption_question(assumption["id"], question["id"])
    store.add_event(wo["id"], "autoreview_asked", {
        "assumption_id": assumption["id"], "n": assumption.get("n"),
        "neo_question_id": question["id"], "early": early})
    log.info("auto-review asked Neo about assumption #%s of %s as question %s",
             assumption.get("n"), wo["id"], question["id"])
    return question
