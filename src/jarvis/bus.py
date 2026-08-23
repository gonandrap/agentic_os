"""The message bus: envelopes addressed to a ROLE, delivered by a pure router.

Nothing in Jarvis addresses anything directly. A work order does not talk to the project
manager and a validation panel does not talk to a work order: every cross-entity message
is an **envelope** posted to a *role* about a *subject*, and this module works out who
fills that role. The sender never names a recipient and never learns who read it.

The point is extension. A new participant costs a routing rule here — one row of the
table below — rather than an edit to everything that might want to reach it.

Three things are load-bearing and easy to lose in a refactor:

* **`post` takes no `kind`.** The kind is derived from the payload's type, so the single
  most likely defect in a hand-rolled bus — a `kind` that disagrees with its payload — is
  unrepresentable rather than validated after the fact. Adding a `kind` parameter back
  for flexibility is that bug, reintroduced.
* **The router, not the sender, decides what happens when a role is unfilled.** A sender
  that has to ask "does the recipient exist?" is a sender coupled to its recipient. See
  `deliver`: a deferral with no manager is filed as a backlog item by the router itself,
  which is exactly today's behaviour.
* **Delivery rides the existing queue.** `deliver` calls `store.queue_message`, and
  `Daemon.deliver_messages` turns that into the next turn on the session exactly as
  `jarvis wo send` does. There is no second dispatch path, for the same reason feature
  orders reused `claim_next_pending` rather than inventing a scheduler.

Layering: this module imports `project_store` and `central_store` and nothing above them.
It must never import `daemon`, `ops`, `panel`, `validation` or `neo` — the daemon is the
only module that knows about the bus and its posters together, and a test walks this
file's AST to keep it that way.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, fields
from typing import TYPE_CHECKING, Any

from . import db
from .project_store import ENVELOPE_ROLES, FO_OPEN_STATUSES, TERMINAL_STATUSES

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .central_store import CentralStore
    from .project_store import ProjectStore

log = logging.getLogger("jarvis.bus")

#: How many routing attempts an envelope gets before INV-ENVELOPE-STUCK steps in. The
#: number is not the point: what matters is that a message nobody will ever receive stops
#: looking like one that was delivered.
DELIVERY_ATTEMPT_CEILING = 5

#: `source` on the `wo_messages` row an envelope becomes. Tells a delivered envelope apart
#: from a `jarvis wo send` in the work order record, which is the only way to read back
#: what the worker was actually reacting to.
MESSAGE_SOURCE = "bus"


class BusError(Exception):
    """A message that cannot be posted or cannot be parsed back out of the database."""


# -- the subject and the typed payloads ---------------------------------------------
#
# The columns are TEXT, so the typing has to live at the boundary and it has to be strong
# there: everything else in this feature stands on this pipe, and a role misspelled at a
# call site must fail at that call site rather than surface weeks later as an
# `undeliverable` row nobody reads.


@dataclass(frozen=True, slots=True)
class Subject:
    """What an envelope is ABOUT: a work order or a feature order, never both."""

    wo_id: str | None = None
    fo_id: str | None = None

    def __post_init__(self) -> None:
        if bool(self.wo_id) == bool(self.fo_id):
            raise BusError("a subject is exactly one of wo_id or fo_id, "
                           f"got wo_id={self.wo_id!r} fo_id={self.fo_id!r}")


@dataclass(frozen=True, slots=True)
class ReviewFeedback:
    """A reviewer's verdict travelling back to whoever must act on it."""

    round: int
    outcome: str               # rejected | escalated
    reason: str
    asks: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DeferralRequest:
    """Work found on the way that is not this work order's job."""

    title: str
    why: str
    neo_question_id: int | None = None


#: kind -> the dataclass that IS that kind. Every entry in `ENVELOPE_KINDS` must appear
#: here; a test walks the tuple rather than listing the kinds, so a kind added without a
#: payload type fails the suite instead of producing an `undeliverable` row months later.
PAYLOADS: dict[str, type] = {
    "review_feedback": ReviewFeedback,
    "deferral_request": DeferralRequest,
}
#: The reverse lookup `post` derives `kind` from. Built from PAYLOADS so the two cannot
#: drift.
KINDS: dict[type, str] = {cls: kind for kind, cls in PAYLOADS.items()}

#: Roles the router can actually resolve — the whole routing table lives in `resolve`.
ADDRESSABLE_ROLES = ("implementor", "manager")
#: Roles that only ever send. `reviewer` has no session of its own: a panel is called,
#: it is not messaged.
SENDER_ONLY_ROLES = ("reviewer",)


def parse_payload(kind: str, payload_json: str | None) -> ReviewFeedback | DeferralRequest:
    """Rebuild a stored payload as the dataclass its `kind` names.

    Raises BusError if it no longer fits — a schema that moved, a hand-written row, a
    kind with no type. An envelope whose payload does not parse is never delivered as a
    malformed message; `deliver` marks it `undeliverable` with the reason in `note`.
    """
    cls = PAYLOADS.get(kind)
    if cls is None:
        raise BusError(f"no payload type is registered for kind {kind!r}")
    data = db.from_json(payload_json, None)
    if not isinstance(data, dict):
        raise BusError(f"payload is not a JSON object: {payload_json!r}")
    # JSON has no tuples, so a `tuple[str, ...]` field comes back as a list. Coerced
    # rather than accepted, because these dataclasses are frozen and compared by value.
    tuple_fields = {f.name for f in fields(cls) if "tuple" in str(f.type)}
    data = {k: (tuple(v) if k in tuple_fields and isinstance(v, list) else v)
            for k, v in data.items()}
    try:
        return cls(**data)  # type: ignore[return-value]
    except TypeError as e:
        raise BusError(f"payload does not fit {cls.__name__}: {e}") from e


# -- posting -------------------------------------------------------------------------


def post(store: ProjectStore, *, subject: Subject, from_role: str, to_role: str,
         payload: ReviewFeedback | DeferralRequest) -> int:
    """Queue an envelope. Returns its id.

    NEVER resolves — resolution is delivery's job, and a sender that resolved would be a
    sender that knows who it is talking to. Raises BusError on an unknown role or a
    payload of no known kind.

    There is no `kind` parameter on purpose: see the module docstring.
    """
    kind = KINDS.get(type(payload))
    if kind is None:
        raise BusError(
            f"{type(payload).__name__} is not a registered payload; a caller hands the "
            f"bus a dataclass, never a bare dict. Known: {sorted(PAYLOADS)}")
    for label, role in (("from_role", from_role), ("to_role", to_role)):
        if role not in ENVELOPE_ROLES:
            raise BusError(f"unknown {label} {role!r}; known roles: {list(ENVELOPE_ROLES)}")
    return store.post_envelope(
        subject_wo_id=subject.wo_id, subject_fo_id=subject.fo_id,
        from_role=from_role, to_role=to_role, kind=kind, payload=asdict(payload))


# -- routing -------------------------------------------------------------------------


def _feature_of(store: ProjectStore, envelope: dict[str, Any]) -> str | None:
    """The feature order this envelope's subject belongs to, if any.

    For a feature-order subject that is the subject itself; for a work-order subject it
    is its `parent_id`, which is NULL for the standalone work orders that are nearly all
    of them.
    """
    if envelope["subject_fo_id"]:
        return envelope["subject_fo_id"]
    try:
        wo = store.get_work_order(envelope["subject_wo_id"])
    except KeyError:
        return None
    return wo.get("parent_id")


def resolve(store: ProjectStore, envelope: dict[str, Any]) -> str | None:
    """Which work order fills `to_role` for this envelope's subject? None if nobody.

    PURE with respect to the bus: it reads, it does not write. The routing table is the
    whole of it —

    | `to_role`     | resolves to                                                  |
    |---------------|--------------------------------------------------------------|
    | `implementor` | the subject work order itself                                 |
    | `manager`     | the `kind='manager'` work order under the subject's feature   |

    A settled work order (completed, cancelled, failed) fills no role: it has no live
    session to resume, so queueing a message for it would be a message nobody reads.
    """
    to_role = envelope["to_role"]
    if to_role == "implementor":
        wo_id = envelope["subject_wo_id"]
        if not wo_id:
            return None  # a feature order has no session of its own to implement it
        try:
            wo = store.get_work_order(wo_id)
        except KeyError:
            return None
        return None if wo["status"] in TERMINAL_STATUSES else wo_id
    if to_role == "manager":
        fo_id = _feature_of(store, envelope)
        if not fo_id:
            return None
        manager = store.manager_work_order(fo_id)
        if not manager or manager["status"] in TERMINAL_STATUSES:
            return None
        return str(manager["id"])
    return None  # a send-only role — nobody is listening on it, by design


# -- delivery ------------------------------------------------------------------------


def render(payload: ReviewFeedback | DeferralRequest) -> str:
    """The text the recipient's session actually receives.

    Says what happened and nothing about who said it: the recipient is not told which
    entity posted the envelope, because knowing would couple it back.
    """
    if isinstance(payload, ReviewFeedback):
        lines = [f"Review feedback (round {payload.round}): {payload.outcome}.", "",
                 payload.reason]
        if payload.asks:
            lines += ["", "What has to change:"]
            lines += [f"- {ask}" for ask in payload.asks]
        lines += ["", "Act on this and continue your work order."]
        return "\n".join(lines)
    lines = [f"Deferral request: {payload.title}", "", payload.why]
    if payload.neo_question_id is not None:
        lines += ["", f"(raised against Neo question {payload.neo_question_id})"]
    return "\n".join(lines)


def deliver(store: ProjectStore, central: CentralStore, envelope: dict[str, Any],
            *, project: str = "") -> str:
    """Route one envelope. Returns its new state.

    `project` is the project's catalog name, needed only to file a backlog item when a
    deferral has no manager to reach. The daemon knows it authoritatively and passes it;
    an empty value is resolved from the central project registry, which is what lets a
    caller holding only a store — `invariants.check_envelopes_move` — route as well.
    """
    env_id = int(envelope["id"])
    # Counted before anything can fail, and outside the delivery transaction, so an
    # envelope that cannot be routed climbs towards the ceiling instead of retrying for
    # ever (see ProjectStore.bump_envelope_attempt).
    store.bump_envelope_attempt(env_id)
    try:
        payload = parse_payload(envelope["kind"], envelope["payload"])
    except BusError as e:
        store.mark_envelope(env_id, "undeliverable", note=str(e))
        return "undeliverable"

    target = resolve(store, envelope)
    if target is None:
        return _unfilled(store, central, envelope, payload, project=project)

    try:
        store.deliver_envelope(env_id, target, render(payload),
                               source=MESSAGE_SOURCE)
    except Exception as e:  # noqa: BLE001 — one bad envelope must not stop the tick
        # The transaction rolled back, so the envelope is exactly as it was: still
        # `queued`, with no message queued against it. It will be retried on the next
        # tick, and INV-ENVELOPE-STUCK picks it up if the retries never take.
        log.warning("envelope %s could not be delivered to %s: %s", env_id, target, e)
        return "queued"
    return "delivered"


def _unfilled(store: ProjectStore, central: CentralStore, envelope: dict[str, Any],
              payload: ReviewFeedback | DeferralRequest, *, project: str) -> str:
    """Nobody fills the role. THE ROUTER decides what happens — never the sender.

    Three cases, and they are different verdicts rather than one shrug:

    * a `deferral_request` with no manager — a work order with no parent feature, which
      is the overwhelmingly common case today — is filed as a backlog item by the router
      itself. That is exactly today's behaviour, preserved;
    * `review_feedback` with no implementor (the work order was cancelled or deleted) is
      `undeliverable`, because feedback that reached nobody must never look like feedback
      that was acted on;
    * a manager that exists but is cancelled while its feature is still open is
      `undeliverable` AND flags the feature: a feature whose manager is gone cannot run
      its loop, and only the user can decide what to do about that.
    """
    env_id = int(envelope["id"])
    to_role = envelope["to_role"]

    if to_role == "manager":
        fo_id = _feature_of(store, envelope)
        manager = store.manager_work_order(fo_id) if fo_id else None
        if manager:  # it exists, so it is settled — resolve would have returned it
            feature = store.get_feature_order(fo_id)  # type: ignore[arg-type]
            note = (f"the manager work order {manager['id']} is {manager['status']}, "
                    f"so nothing can act on this")
            store.mark_envelope(env_id, "undeliverable", note=note)
            if feature["status"] in FO_OPEN_STATUSES:
                store.flag_feature_attention(
                    fo_id,  # type: ignore[arg-type]
                    f"its manager work order is {manager['status']} — this feature "
                    f"cannot receive messages until one owns it again")
            return "undeliverable"

    if envelope["kind"] == "deferral_request" and isinstance(payload, DeferralRequest):
        name = project or project_name(store, central)
        item = central.add_backlog(name, payload.title, payload.why)
        store.mark_envelope(
            env_id, "handled_by_router",
            note=f"no {to_role} to reach; filed backlog item {item['id']}")
        return "handled_by_router"

    store.mark_envelope(
        env_id, "undeliverable",
        note=f"no {to_role} for this subject, and nothing this router can do about it")
    return "undeliverable"


def project_name(store: ProjectStore, central: CentralStore) -> str:
    """The catalog name of the project this store belongs to.

    The daemon passes the name it already holds; this is the fallback for a caller that
    has only a store, and it is a lookup rather than a guess — the registry maps name to
    path, so the path resolves back. A project not in the registry falls back to its
    directory name, which is what `jarvis adopt` would have named it.

    The lookup itself lives on `CentralStore`: the validation panel needs the same answer
    from the same starting point, and it may not import this module (a panel returns a
    value; posting is the round machine's job). One implementation, two callers.
    """
    return central.project_name_for_path(store.project_path)
