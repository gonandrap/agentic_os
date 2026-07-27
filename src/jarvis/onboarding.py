"""Onboarding: teaching Jarvis how sessions are launched in a given project.

`jarvis adopt` prepares a project's files. This prepares its *launcher*: a bootstrap
work order whose deliverable is `.jarvis/launcher.json`, produced by an agent session
that interviews the user about their wrapper. The prompt below is deliberately nothing
like a worker prompt — there is no branch, no PR, no code to write; there is a person
in the room and one artifact to agree on.

The verified state of each project's contract lives in the central store (never a
sidecar file), so `jarvis status` and the daemon can see staleness without reading
project directories.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import launcher as launcher_mod
from .central_store import CentralStore

ASSETS = Path(__file__).parent / "assets"
PROTOCOL_DOC = ASSETS / "launcher-protocol.md"

STATE_PREFIX = "launcher:"
ONBOARDING_DIR = ".jarvis/onboarding"


# -- persisted launcher state --------------------------------------------------------


def launcher_state(project_name: str, central: CentralStore | None = None) -> dict[str, Any]:
    """What the OS remembers about this project's launcher (verification, failures)."""
    own = central is None
    central = central or CentralStore()
    try:
        raw = central.get_state(STATE_PREFIX + project_name)
    finally:
        if own:
            central.close()
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}


def _write_state(project_name: str, state: dict[str, Any]) -> dict[str, Any]:
    central = CentralStore()
    try:
        central.set_state(STATE_PREFIX + project_name, json.dumps(state))
    finally:
        central.close()
    return state


def record_verification(project_name: str, report: dict[str, Any]) -> dict[str, Any]:
    """Persist a verify run. Only a passing `--live` run counts as verification —
    a static check proves the file parses, not that a session ever starts."""
    state = launcher_state(project_name)
    state.update({
        "source": report.get("source"),
        "name": report.get("launcher"),
        "fingerprint": report.get("fingerprint"),
        "last_check_at": time.time(),
        "last_check_ok": bool(report.get("ok")),
    })
    if report.get("live") and report.get("ok"):
        state["verified_at"] = time.time()
        state["verified_fingerprint"] = report.get("fingerprint")
        state["spawn_failures"] = 0
    return _write_state(project_name, state)


def record_spawn_outcome(project_name: str, ok: bool, error: str = "") -> None:
    """Count consecutive spawn failures — the signal that a contract has gone bad in a
    way no file hash reveals (the wrapper still exists, it just doesn't work any more)."""
    state = launcher_state(project_name)
    if ok:
        if not state.get("spawn_failures"):
            return
        state["spawn_failures"] = 0
        state.pop("last_spawn_error", None)
    else:
        state["spawn_failures"] = int(state.get("spawn_failures") or 0) + 1
        state["last_spawn_error"] = error[:500]
    _write_state(project_name, state)


def launcher_health(project: Any) -> dict[str, Any]:
    """Everything worth knowing about a project's launcher, in one dict.

    `problems` is the list that becomes attention items: each entry is a reason to run
    a fresh onboarding session, phrased for the user rather than for the log.
    """
    state = launcher_state(project.name)
    problems: list[str] = []
    info: dict[str, Any] = {
        "project": project.name,
        "launcher": "native",
        "source": "built-in",
        "capabilities": launcher_mod.Capabilities().as_dict(),
        "verified_at": state.get("verified_at"),
        "spawn_failures": int(state.get("spawn_failures") or 0),
        "drift": [],
    }
    try:
        lch = launcher_mod.launcher_for(project)
    except launcher_mod.LauncherError as e:
        info["error"] = str(e)
        problems.append(str(e))
        info["problems"] = problems
        return info

    info["launcher"] = lch.name
    info["source"] = lch.source
    info["capabilities"] = lch.capabilities.as_dict()
    contract = getattr(lch, "contract", None)
    if contract is None:
        # The native launcher has no contract to go stale: it ships with the OS and is
        # covered by the test suite, so it is never nagged about.
        info["problems"] = problems
        return info

    info["fingerprint"] = launcher_mod.fingerprint(contract)
    drift = launcher_mod.source_drift(contract)
    info["drift"] = drift
    if drift:
        problems.append(
            "the wrapper this contract was derived from has changed: "
            + ", ".join(drift)
        )
    verified_at = state.get("verified_at")
    if not verified_at or state.get("verified_fingerprint") != info["fingerprint"]:
        problems.append("this launcher contract has never passed a live verification")
    elif time.time() - verified_at > launcher_mod.REVERIFY_AFTER_DAYS * 86400:
        days = int((time.time() - verified_at) / 86400)
        problems.append(f"last verified {days} days ago — wrappers move, re-check it")
    if info["spawn_failures"] >= launcher_mod.SPAWN_FAILURES_BEFORE_DRIFT:
        problems.append(
            f"{info['spawn_failures']} consecutive spawns failed through this contract"
            + (f" (last: {state['last_spawn_error'][:120]})"
               if state.get("last_spawn_error") else "")
        )
    info["problems"] = problems
    return info


# -- the bootstrap prompt --------------------------------------------------------------


def build_bootstrap_prompt(project: Any, wo_id: str, existing: dict[str, Any] | None = None,
                           reason: str = "") -> str:
    """The prompt for an onboarding session. Not a work order: an interview.

    Everything the session needs travels in here — the protocol, the target path, the
    verification commands — because the person running it may paste this into a plain
    session with no Jarvis context at all.
    """
    contract_path = Path(project.path) / launcher_mod.PROJECT_CONTRACT
    parts = [
        f"You are running a **Jarvis OS launcher onboarding session** for the project "
        f"`{project.name}` at `{project.path}`.",
        "",
        "This is NOT a work order. There is no feature to build, no branch to open and "
        "no PR to file. The user is present and you are interviewing them.",
        "",
        "# Why you are here",
        "",
        "Jarvis runs background agent sessions to do work. It ships knowing exactly one "
        "way to start one (`claude --bg …`), and that is not how sessions are started "
        "here. Your job is to find out how they *are* started on this machine, for this "
        "project, and to write that down as a launcher contract Jarvis can execute.",
        "",
        f"Deliverable: `{contract_path}` — one JSON file, conforming to the protocol "
        f"below. Nothing else. Do not refactor the project, do not write helper "
        f"libraries, do not commit anything unless the user asks.",
        "",
        "# How to run the session",
        "",
        "1. **Ask, do not guess.** Open with: *how do you start a background agent "
        "session in this project today?* Get the literal command line.",
        "2. **Make them show you.** For each of the five verbs, ask the user to run the "
        "real command and paste the real output. Parse what you were shown, not what "
        "you would expect a tool like that to print. If you cannot see real output for "
        "a verb, say so and record it in `provenance.notes` rather than inventing a "
        "plausible shape.",
        "3. **Cover the whole loop.** Spawning is the easy half. Jarvis also needs to "
        "know which sessions exist and what state each is in, how to read back the "
        "final message of a finished turn, how to deliver a follow-up message, and how "
        "to stop a session. A contract with only `spawn` leaves the OS blind.",
        "4. **Declare capabilities honestly.** A false `true` is worse than a `false`: "
        "Jarvis degrades deliberately around a missing capability, but silently breaks "
        "around a claimed one that does not exist.",
        "5. **Record where it came from.** Put the wrapper's own file path(s) in "
        "`provenance.sources` with `\"sha256\": \"auto\"`, and its version string in "
        "`provenance.wrapper_version`. That is what lets Jarvis notice later that the "
        "wrapper moved and ask for you again.",
        "",
        "# Finishing",
        "",
        "```bash",
        f"jarvis launcher verify {project.name}          # static: schema, placeholders, binaries",
        f"jarvis launcher verify {project.name} --live   # spawns a real throwaway session",
        "```",
        "",
        "Fix whatever the static check reports, then run the live check — it is the only "
        "thing that proves the contract, and until it passes once the OS reports this "
        "project as unverified. If the live check fails, the contract is wrong: go back "
        "to the user with the exact error rather than loosening the check.",
        "",
        f"When the live check passes, close the session with:",
        "",
        "```bash",
        f"jarvis wo finish {wo_id} --summary \"launcher contract for {project.name}: "
        f"<wrapper name>, verified live\"",
        "```",
        "",
        "Then write your full report as your last message: which verbs are covered, "
        "which capabilities are false and why, what you could not verify, and the "
        "absolute path of the contract file. Nobody will read this session afterwards — "
        "that report is the whole record.",
        "",
        "If the user cannot answer something and you cannot determine it, stop and say "
        "exactly what is missing. An incomplete contract that is honest about its gaps "
        "is useful; a complete-looking one that is guessed will fail at 3am.",
    ]
    if reason:
        parts += ["", "# Why this is being re-run now", "", reason]
    if existing:
        parts += [
            "",
            "# The contract in force today",
            "",
            "Amend this rather than starting over — keep what still holds, and change "
            "only what the interview shows to be wrong:",
            "",
            "```json",
            json.dumps(existing, indent=2),
            "```",
        ]
    parts += ["", "---", "", _protocol_text()]
    return "\n".join(parts)


def _protocol_text() -> str:
    try:
        return PROTOCOL_DOC.read_text()
    except OSError:  # pragma: no cover - the asset ships with the package
        return "(launcher-protocol.md missing from this Jarvis installation)"


# -- operations -------------------------------------------------------------------------


def start_onboarding(project: Any, reason: str = "", dispatch: bool | None = None
                     ) -> dict[str, Any]:
    """Raise a bootstrap work order for a project and return how to run it.

    The first bootstrap cannot be dispatched: Jarvis does not yet know how to start a
    session here, which is the entire point of the exercise. So the prompt is written
    to disk and handed to the user, and the work order waits. Once a contract exists and
    has been verified, a re-onboarding is an ordinary dispatch like anything else.
    """
    from .project_store import ProjectStore

    health = launcher_health(project)
    verified = bool(health.get("verified_at")) and not health.get("error")
    if dispatch is None:
        dispatch = verified

    existing: dict[str, Any] | None = None
    source = health.get("source")
    if source and source != "built-in":
        try:
            existing = launcher_mod.load_contract(source)
        except launcher_mod.LauncherError:
            existing = None

    store = ProjectStore(project.path)
    try:
        title = (f"Re-onboard the launcher for {project.name}"
                 if existing else f"Onboard the launcher for {project.name}")
        wo = store.create_work_order(
            title=title,
            description=(reason or "Establish how background agent sessions are "
                                   "launched for this project.") +
                        "\n\n(bootstrap work order — the prompt is generated by "
                        "`jarvis onboard`, not from this description)",
            origin="jarvis",
            kind="bootstrap",
        )
        prompt = build_bootstrap_prompt(project, wo["id"], existing=existing, reason=reason)
        prompt_path = Path(project.path) / ONBOARDING_DIR / f"{wo['id']}.md"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(prompt)
        store.add_event(wo["id"], "bootstrap_prepared", {
            "prompt_path": str(prompt_path), "dispatch": bool(dispatch),
            "amending": existing is not None,
        })
        if not dispatch:
            # Not pending: nothing may pick this up and try to spawn it with a launcher
            # that does not work yet. It is waiting on a human, and says so.
            store.set_status(wo["id"], "waiting_input")
            store.flag_attention(
                wo["id"],
                "bootstrap session must be started by hand — see "
                f"{prompt_path}",
            )
    finally:
        store.close()

    return {
        "project": project.name,
        "wo_id": wo["id"],
        "prompt_path": str(prompt_path),
        "dispatch": bool(dispatch),
        "amending": existing is not None,
        "launcher": health.get("launcher"),
        "note": (
            "jarvisd will dispatch this through the existing verified launcher"
            if dispatch else
            "start a session in the project yourself and give it this file's contents "
            "as its prompt — Jarvis cannot spawn one here until the contract exists"
        ),
    }


def save_contract(project: Any, contract: dict[str, Any]) -> Path:
    """Write a contract to the project's `.jarvis/launcher.json`, hashing its sources.

    Used by `jarvis launcher install`; an onboarding session may equally write the file
    itself, in which case `jarvis launcher verify` stamps the digests on first read.
    """
    problems = launcher_mod.validate_contract(contract)
    if problems:
        raise launcher_mod.LauncherError(
            "contract is invalid:\n  - " + "\n  - ".join(problems))
    launcher_mod.stamp_source_digests(contract)
    path = Path(project.path) / launcher_mod.PROJECT_CONTRACT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(contract, indent=2) + "\n")
    return path
