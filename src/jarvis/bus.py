"""The message bus: envelopes addressed to a ROLE, delivered by a pure router"""

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

#: How many routing attempts an envelope gets before INV-ENVELOPE-STUCK steps in
DELIVERY_ATTEMPT_CEILING = 5

#: `source` on the `wo_messages` row an envelope becomes
MESSAGE_SOURCE = "bus"


class BusError(Exception):
    """A message that cannot be posted or cannot be parsed back out of the database"""


# -- the subject and the typed payloads


@dataclass(frozen=True, slots=True)
class Subject:
    """What an envelope is ABOUT: a work order or a feature order, never both"""

    wo_id: str | None = None
    fo_id: str | None = None

    def __post_init__(self) -> None:
        if bool(self.wo_id) == bool(self.fo_id):
            raise BusError("a subject is exactly one of wo_id or fo_id, "
                           f"got wo_id={self.wo_id!r} fo_id={self.fo_id!r}")


@dataclass(frozen=True, slots=True)
class ReviewFeedback:
    """A reviewer's verdict travelling back to whoever must act on it"""

    round: int
    outcome: str               # rejected | escalated
    reason: str
    asks: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DeferralRequest:
    """Work found on the way that is not this work order's job"""

    title: str
    why: str
    neo_question_id: int | None = None
    description: str = ""


@dataclass(frozen=True, slots=True)
class ChildrenLanded:
    """The work orders filed after round `round` have all completed"""

    round: int
    children: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True, slots=True)
class AssumptionObjection:
    """The OS disagreeing with an assumption while the worker is still typing"""

    assumption_n: int
    reason: str
    question_id: int | None = None


#: kind -> the dataclass that IS that kind
PAYLOADS: dict[str, type] = {
    "review_feedback": ReviewFeedback,
    "deferral_request": DeferralRequest,
    "children_landed": ChildrenLanded,
    "assumption_objection": AssumptionObjection,
}
#: Every payload type, as one name
Payload = ReviewFeedback | DeferralRequest | ChildrenLanded | AssumptionObjection

#: The reverse lookup `post` derives `kind` from
KINDS: dict[type, str] = {cls: kind for kind, cls in PAYLOADS.items()}

#: Roles the router can actually resolve — the whole routing table lives in `resolve`
ADDRESSABLE_ROLES = ("implementor", "manager")
#: Roles that only ever send
SENDER_ONLY_ROLES = ("reviewer", "reconciler")


def parse_payload(kind: str, payload_json: str | None) -> Payload:
    """Rebuild a stored payload as the dataclass its `kind` names"""
    cls = PAYLOADS.get(kind)
    if cls is None:
        raise BusError(f"no payload type is registered for kind {kind!r}")
    data = db.from_json(payload_json, None)
    if not isinstance(data, dict):
        raise BusError(f"payload is not a JSON object: {payload_json!r}")
    # JSON has no tuples, so a `tuple[str, ...]` field comes back as a list
    tuple_fields = {f.name for f in fields(cls) if "tuple" in str(f.type)}
    data = {k: (tuple(v) if k in tuple_fields and isinstance(v, list) else v)
            for k, v in data.items()}
    try:
        return cls(**data)  # type: ignore[return-value]
    except TypeError as e:
        raise BusError(f"payload does not fit {cls.__name__}: {e}") from e


# -- posting


def post(store: ProjectStore, *, subject: Subject, from_role: str, to_role: str,
         payload: Payload) -> int:
    """Queue an envelope"""
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


# -- routing


def _feature_of(store: ProjectStore, envelope: dict[str, Any]) -> str | None:
    """The feature order this envelope's subject belongs to, if any"""
    if envelope["subject_fo_id"]:
        return envelope["subject_fo_id"]
    try:
        wo = store.get_work_order(envelope["subject_wo_id"])
    except KeyError:
        return None
    return wo.get("parent_id")


def resolve(store: ProjectStore, envelope: dict[str, Any]) -> str | None:
    """Which work order fills `to_role` for this envelope's subject?"""
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


# -- delivery


def origin_note(payload: DeferralRequest) -> str:
    """The `origin_note` a deferral becomes, wherever it is filed from"""
    note = payload.why
    if payload.neo_question_id is not None:
        note = f"{note} (Neo question {payload.neo_question_id})"
    return note


def _q(value: str) -> str:
    """Double-quote a value for the shell command a recipient is shown"""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render(payload: Payload, *, project: str = "",
           origin_wo_id: str | None = None, origin_fo_id: str | None = None) -> str:
    """The text the recipient's session actually receives"""
    if isinstance(payload, ReviewFeedback):
        lines = [f"Review feedback (round {payload.round}): {payload.outcome}.", "",
                 payload.reason]
        if payload.asks:
            lines += ["", "What has to change:"]
            lines += [f"- {ask}" for ask in payload.asks]
        lines += ["", "Act on this and continue your work order."]
        return "\n".join(lines)
    if isinstance(payload, AssumptionObjection):
        # §6 of docs/superpowers/specs/2026-09-23-an-assumption-judged-while-the-worker- still-runs.md: a...
        lines = [f"The OS disagrees with assumption #{payload.assumption_n} you "
                 f"recorded.", "", payload.reason, "",
                 "This is the OS's own reading (Neo), not the user's words, and it "
                 "settles nothing: the assumption is still pending their decision. Act "
                 "on it in the work you are doing now — nothing is being reopened and "
                 "you are not being asked to report back."]
        if payload.question_id is not None:
            lines += ["", f"(Neo question {payload.question_id})"]
        return "\n".join(lines)
    if isinstance(payload, ChildrenLanded):
        lines = [f"The work orders you filed after round {payload.round} have landed."]
        if payload.children:
            lines += [""] + [f"- {wo_id}" for wo_id in payload.children]
        if payload.note:
            lines += ["", payload.note]
        return "\n".join(lines)
    lines = [f"Deferral request: {payload.title}", "", payload.why]
    if payload.description:
        lines += ["", payload.description]
    if payload.neo_question_id is not None:
        lines += ["", f"(raised against Neo question {payload.neo_question_id})"]
    cmd = [f"    jarvis backlog add {project or '<project>'} {_q(payload.title)} \\"]
    if payload.description:
        cmd.append(f"        -d {_q(payload.description)} \\")
    if origin_wo_id:
        cmd.append(f"        --origin-wo {origin_wo_id} \\")
    if origin_fo_id:
        cmd.append(f"        --origin-fo {origin_fo_id} \\")
    cmd.append(f"        --origin-note {_q(origin_note(payload))}")
    lines += ["", "File this on the backlog with where it came from recorded — the "
                  "flags ARE the record, so run it as it stands:", "", *cmd, "",
              "Filing it is the whole action: you are not being asked to schedule it, "
              "and you do not report back."]
    return "\n".join(lines)


def deliver(store: ProjectStore, central: CentralStore, envelope: dict[str, Any],
            *, project: str = "") -> str:
    """Route one envelope"""
    env_id = int(envelope["id"])
    # Counted before anything can fail
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
        store.deliver_envelope(env_id, target,
                               render(payload, **_context(store, central, envelope,
                                                          payload, project=project)),
                               source=MESSAGE_SOURCE)
    except Exception as e:  # noqa: BLE001 — one bad envelope must not stop the tick
        # The transaction rolled back, so the envelope is exactly as it was: still `queued`
        log.warning("envelope %s could not be delivered to %s: %s", env_id, target, e)
        return "queued"
    return "delivered"


def nothing_queued_to_manager(store: ProjectStore, fo_id: str,
                              manager_wo_id: str) -> bool:
    """Has nobody spoken to this feature's manager? §2.2 of docs/superpowers/specs/..."""
    if store.queued_messages(manager_wo_id):
        return False
    return not any(e["state"] == "queued" and e["to_role"] == "manager"
                   for e in store.envelopes(subject_fo_id=fo_id))


def _context(store: ProjectStore, central: CentralStore, envelope: dict[str, Any],
             payload: Payload, *,
             project: str) -> dict[str, Any]:
    """What `render` needs beyond the payload, for the kinds that need anything"""
    if not isinstance(payload, DeferralRequest):
        return {}
    return {"project": project or project_name(store, central),
            "origin_wo_id": envelope["subject_wo_id"],
            "origin_fo_id": _feature_of(store, envelope)}


def _unfilled(store: ProjectStore, central: CentralStore, envelope: dict[str, Any],
              payload: Payload, *, project: str) -> str:
    """Nobody fills the role"""
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
        # The SAME three columns the manager's `jarvis backlog add` invocation fills in
        item = central.add_backlog(
            name, payload.title, payload.description or payload.why,
            origin_wo_id=envelope["subject_wo_id"],
            origin_fo_id=_feature_of(store, envelope),
            origin_note=origin_note(payload))
        store.mark_envelope(
            env_id, "handled_by_router",
            note=f"no {to_role} to reach; filed backlog item {item['id']}")
        return "handled_by_router"

    store.mark_envelope(
        env_id, "undeliverable",
        note=f"no {to_role} for this subject, and nothing this router can do about it")
    return "undeliverable"


def project_name(store: ProjectStore, central: CentralStore) -> str:
    """The catalog name of the project this store belongs to"""
    return central.project_name_for_path(store.project_path)
