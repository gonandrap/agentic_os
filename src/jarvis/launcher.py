"""Launcher: how Jarvis starts, watches, messages and stops a worker session.

Everything the OS knows about running an agent is expressed as five verbs — spawn,
list, result, send, stop. `claude --bg` is one implementation of those verbs
(`NativeLauncher`); a project whose sessions come from somebody's own wrapper supplies
another as *data*, a JSON contract negotiated in an onboarding session
(`ContractLauncher`, protocol in `assets/launcher-protocol.md`).

Nothing above this module may call `claude_cli` for session lifecycle: the whole point
is that dispatch, the daemon and ops speak verbs, not flags.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from . import claude_cli
from .claude_cli import BgSession

SCHEMA_VERSION = 1

# Where a contract may live, in resolution order (catalog override comes first, and is
# handled by `launcher_for` since it needs the project spec).
PROJECT_CONTRACT = ".jarvis/launcher.json"
FLEET_CONTRACT = "launcher.json"  # under $JARVIS_HOME

VERBS = ("spawn", "list", "result", "send", "stop")
REQUIRED_VERBS = ("spawn", "list")

# Placeholders each verb may reference. A typo has to fail at verify time, in front of
# the person who wrote it, rather than at 3am on a dispatch.
PLACEHOLDERS: dict[str, frozenset[str]] = {
    "spawn": frozenset({
        "prompt", "cwd", "name", "model", "effort", "permission_mode",
        "append_system_prompt", "settings_file", "worktree", "resume_session_id",
        "add_dirs", "item", "wo_id", "project",
    }),
    "list": frozenset({"cwd"}),
    "result": frozenset({"job_id"}),
    "send": frozenset({"session_id", "message", "cwd", "job_id"}),
    "stop": frozenset({"job_id"}),
}

# Probe used by `verify --live`: a real session, but one that must not touch anything.
PROBE_PROMPT = (
    "This is an automated Jarvis launcher self-test. Reply with exactly LAUNCHER-OK "
    "and end your turn. Do not read files, run commands, or modify anything."
)
PROBE_NAME = "[jarvis] launcher self-test"

# A contract goes stale on its own: wrappers change under it, so a verification that
# nobody has repeated in a month is not evidence any more.
REVERIFY_AFTER_DAYS = 30
# Consecutive spawn failures before the OS stops blaming the work order and starts
# blaming the contract.
SPAWN_FAILURES_BEFORE_DRIFT = 3

_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")


class LauncherError(RuntimeError):
    """A launcher could not do what was asked (bad contract, or the wrapper failed)."""


@contextmanager
def _as_launcher_error():
    """Re-raise a claude CLI failure as a launcher failure, preserving the cause."""
    try:
        yield
    except claude_cli.ClaudeCliError as e:
        raise LauncherError(str(e)) from e


@dataclass(frozen=True)
class Capabilities:
    """What the launcher can do. Absent capabilities are degraded around, never faked."""
    worktree: bool = True
    resume: bool = True
    settings_file: bool = True
    add_dirs: bool = True
    hooks: bool = True

    @classmethod
    def from_dict(cls, data: Any) -> Capabilities:
        if not isinstance(data, dict):
            return cls()
        known = {f: bool(data[f]) for f in cls.__dataclass_fields__ if f in data}
        return replace(cls(), **known)

    def as_dict(self) -> dict[str, bool]:
        return {f: getattr(self, f) for f in self.__dataclass_fields__}


# -- contract validation -------------------------------------------------------------


def _iter_placeholders(item: Any) -> list[str]:
    """Placeholder names referenced anywhere inside a command spec fragment."""
    if isinstance(item, str):
        return _PLACEHOLDER_RE.findall(item)
    if isinstance(item, dict):
        names = [item["if"]] if isinstance(item.get("if"), str) else []
        for arg in item.get("args") or []:
            names += _iter_placeholders(arg)
        return names
    if isinstance(item, list):
        return [n for sub in item for n in _iter_placeholders(sub)]
    return []


def _validate_command(verb: str, command: Any, problems: list[str]) -> None:
    if not isinstance(command, list) or not command:
        problems.append(f"{verb}.command must be a non-empty list")
        return
    for i, item in enumerate(command):
        if isinstance(item, str):
            continue
        if isinstance(item, dict):
            if not isinstance(item.get("if"), str) or not item["if"]:
                problems.append(f'{verb}.command[{i}] needs a non-empty "if"')
            if not isinstance(item.get("args"), list) or not item["args"]:
                problems.append(f'{verb}.command[{i}] needs a non-empty "args" list')
            continue
        problems.append(f"{verb}.command[{i}] must be a string or an "
                        f'{{"if": …, "args": […]}} group')
    allowed = PLACEHOLDERS[verb]
    for name in _iter_placeholders(command):
        if name not in allowed:
            problems.append(f"{verb}: unknown placeholder {{{name}}} "
                            f"(allowed: {', '.join(sorted(allowed))})")


def validate_contract(data: Any) -> list[str]:
    """Everything wrong with a contract, in one pass. Empty list = usable."""
    problems: list[str] = []
    if not isinstance(data, dict):
        return ["contract must be a JSON object"]
    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        problems.append(f"schema_version must be {SCHEMA_VERSION} (got {version!r})")
    if not isinstance(data.get("name"), str) or not data.get("name"):
        problems.append("name is required")
    for verb in REQUIRED_VERBS:
        if not isinstance(data.get(verb), dict):
            problems.append(f"{verb} section is required")
    for verb in VERBS:
        spec = data.get(verb)
        if spec is None:
            continue
        if not isinstance(spec, dict):
            problems.append(f"{verb} must be an object")
            continue
        if verb == "result" and "file" in spec:
            for name in _PLACEHOLDER_RE.findall(str(spec["file"])):
                if name not in PLACEHOLDERS["result"]:
                    problems.append(f"result.file: unknown placeholder {{{name}}}")
            continue
        _validate_command(verb, spec.get("command"), problems)
    lst = data.get("list")
    if isinstance(lst, dict):
        sessions = lst.get("sessions")
        if sessions is not None and not isinstance(sessions, dict):
            problems.append("list.sessions must be an object")
        if lst.get("scope") not in (None, "cwd", "global"):
            problems.append('list.scope must be "cwd" or "global"')
        state_map = lst.get("state_map") or {}
        if not isinstance(state_map, dict):
            problems.append("list.state_map must be an object")
        elif "running" in set(state_map.values()):
            # The bug this repo already paid for once, now unrepeatable by construction.
            problems.append(
                'list.state_map maps a state to "running", which is a Jarvis work-order '
                'word, not a session state — use "working"'
            )
    caps = data.get("capabilities")
    if caps is not None and not isinstance(caps, dict):
        problems.append("capabilities must be an object")
    return problems


def load_contract(path: str | Path) -> dict[str, Any]:
    """Read + validate a contract file. Raises LauncherError with every problem."""
    p = Path(path).expanduser()
    try:
        data = json.loads(p.read_text())
    except FileNotFoundError as e:
        raise LauncherError(f"launcher contract not found: {p}") from e
    except json.JSONDecodeError as e:
        raise LauncherError(f"launcher contract {p} is not valid JSON: {e}") from e
    problems = validate_contract(data)
    if problems:
        raise LauncherError(f"launcher contract {p} is invalid:\n  - "
                            + "\n  - ".join(problems))
    return data


def fingerprint(contract: dict[str, Any]) -> str:
    """Stable digest of a contract's *behaviour* — provenance notes are excluded so
    re-recording who wrote it doesn't read as a change to what it does."""
    body = {k: v for k, v in contract.items() if k != "provenance"}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]


def file_digest(path: str | Path) -> str | None:
    try:
        return hashlib.sha256(Path(path).expanduser().read_bytes()).hexdigest()
    except OSError:
        return None


def stamp_source_digests(contract: dict[str, Any]) -> dict[str, Any]:
    """Fill in `provenance.sources[].sha256` for entries left as "auto" (or missing).

    The onboarding session names the files its contract was derived from; hashing them
    is the OS's job, so a session can't get it subtly wrong.
    """
    prov = contract.setdefault("provenance", {})
    for src in prov.get("sources") or []:
        if not isinstance(src, dict) or not src.get("path"):
            continue
        if src.get("sha256") in (None, "", "auto"):
            src["sha256"] = file_digest(src["path"]) or "missing"
    return contract


def source_drift(contract: dict[str, Any]) -> list[str]:
    """Provenance sources whose content no longer matches what was recorded."""
    drifted = []
    for src in (contract.get("provenance") or {}).get("sources") or []:
        if not isinstance(src, dict) or not src.get("path") or not src.get("sha256"):
            continue
        current = file_digest(src["path"])
        if current is None:
            drifted.append(f"{src['path']} (gone)")
        elif src["sha256"] not in ("auto", "missing") and current != src["sha256"]:
            drifted.append(src["path"])
    return drifted


# -- templating ------------------------------------------------------------------------


def _substitute(text: str, variables: dict[str, Any]) -> str:
    return _PLACEHOLDER_RE.sub(
        lambda m: "" if variables.get(m.group(1)) in (None, False)
        else str(variables[m.group(1)]),
        text,
    )


def render_command(command: list[Any], variables: dict[str, Any]) -> list[str]:
    """Turn a command template plus a variable map into an argv list.

    Two rules carry the whole design:
      * a conditional group is included only when its variable is non-empty (and
        repeats per element when that variable is a list, with `{item}` bound);
      * a plain item that is *nothing but* an empty placeholder disappears, so
        `"{model}"` never becomes an empty argument the wrapper has to defend against.
    """
    argv: list[str] = []
    for item in command:
        if isinstance(item, dict):
            value = variables.get(item["if"])
            if not value:
                continue
            if isinstance(value, (list, tuple)):
                for element in value:
                    scoped = {**variables, "item": element}
                    argv += [_substitute(str(a), scoped) for a in item["args"]]
            else:
                argv += [_substitute(str(a), variables) for a in item["args"]]
            continue
        text = str(item)
        match = _PLACEHOLDER_RE.fullmatch(text)
        if match and not variables.get(match.group(1)):
            continue
        argv.append(_substitute(text, variables))
    return argv


def _dig(data: Any, path: str | None) -> Any:
    """Walk a dotted path into parsed JSON. Empty path = the document itself."""
    if not path:
        return data
    for part in path.split("."):
        if isinstance(data, list):
            try:
                data = data[int(part)]
                continue
            except (ValueError, IndexError):
                return None
        if not isinstance(data, dict):
            return None
        data = data.get(part)
    return data


def extract(spec: Any, stdout: str) -> Any:
    """Pull a value out of a command's stdout per an extraction spec."""
    if not isinstance(spec, dict):
        return None
    source = spec.get("from", "stdout")
    if source == "stdout_json":
        try:
            return _dig(json.loads(stdout or "null"), spec.get("path"))
        except json.JSONDecodeError as e:
            raise LauncherError(f"expected JSON on stdout: {e}; got {stdout[:200]!r}") from e
    if spec.get("regex"):
        m = re.search(spec["regex"], stdout or "")
        if not m:
            return None
        return m.group(1) if m.groups() else m.group(0)
    return (stdout or "").strip() or None


# -- the launchers ------------------------------------------------------------------------


class NativeLauncher:
    """`claude --bg` — what the OS has always done, now behind the same five verbs.

    Every failure surfaces as `LauncherError`, including the `ClaudeCliError`s from
    below: callers above this module handle "the launcher failed", and must not have to
    know which launcher is in play to catch it.
    """

    kind = "native"
    name = "native"
    source = "built-in"
    capabilities = Capabilities()
    roster_scope = "global"
    contract: dict[str, Any] | None = None

    def supports(self, verb: str) -> bool:
        return verb in VERBS

    def spawn(self, prompt: str, cwd: Path, name: str, **kw: Any) -> str | None:
        kw.pop("env", None)  # native carries env through the settings file
        kw.pop("wo_id", None)
        kw.pop("project", None)
        with _as_launcher_error():
            return claude_cli.spawn_background(prompt=prompt, cwd=cwd, name=name, **kw)

    def roster(self, cwd: Path | None = None) -> list[BgSession]:
        with _as_launcher_error():
            return claude_cli.list_background_sessions()

    def result(self, job_id: str) -> tuple[str | None, str | None]:
        return claude_cli.job_result(job_id)

    def send(self, session_id: str, message: str, cwd: Path,
             job_id: str | None = None) -> str:
        with _as_launcher_error():
            return claude_cli.send_to_session(session_id, message, cwd=cwd, bg_id=job_id)

    def stop(self, job_id: str) -> bool:
        return claude_cli.stop_session(job_id)

    def available(self) -> bool:
        return claude_cli.available()


class ContractLauncher:
    """A launcher described entirely by a JSON contract (see launcher-protocol.md)."""

    kind = "contract"

    def __init__(self, contract: dict[str, Any], source: str = "?"):
        self.contract = contract
        self.source = source
        self.name = contract.get("name") or "unnamed"
        self.capabilities = Capabilities.from_dict(contract.get("capabilities"))
        self.roster_scope = (contract.get("list") or {}).get("scope", "cwd")

    # -- plumbing ---------------------------------------------------------------

    def _spec(self, verb: str) -> dict[str, Any] | None:
        spec = self.contract.get(verb)
        return spec if isinstance(spec, dict) else None

    def supports(self, verb: str) -> bool:
        return self._spec(verb) is not None

    def _exec(self, verb: str, variables: dict[str, Any], cwd: Path | None = None,
              timeout: int = 120, env: dict[str, str] | None = None) -> str:
        spec = self._spec(verb)
        if spec is None:
            raise LauncherError(f"launcher {self.name!r} has no {verb!r} verb")
        argv = render_command(spec["command"], variables)
        proc_env = os.environ.copy()
        proc_env.update(env or {})
        try:
            proc = subprocess.run(argv, cwd=cwd, env=proc_env, capture_output=True,
                                  text=True, timeout=timeout)
        except FileNotFoundError as e:
            raise LauncherError(f"launcher {self.name!r}: `{argv[0]}` not found") from e
        except subprocess.TimeoutExpired as e:
            raise LauncherError(
                f"launcher {self.name!r}: `{' '.join(argv[:3])}…` timed out after {timeout}s"
            ) from e
        if proc.returncode != 0:
            raise LauncherError(
                f"launcher {self.name!r}: `{' '.join(argv[:4])}…` failed "
                f"(rc={proc.returncode}): "
                f"{proc.stderr.strip()[:500] or proc.stdout.strip()[:500]}"
            )
        return proc.stdout

    # -- verbs ------------------------------------------------------------------

    def spawn(self, prompt: str, cwd: Path, name: str, model: str | None = None,
              effort: str | None = None, permission_mode: str | None = None,
              append_system_prompt: str | None = None, worktree: str | None = None,
              settings_file: Path | None = None, resume_session_id: str | None = None,
              add_dirs: list[Path] | None = None, env: dict[str, str] | None = None,
              wo_id: str | None = None, project: str | None = None) -> str | None:
        variables = {
            "prompt": prompt, "cwd": str(cwd), "name": name, "model": model,
            "effort": effort, "permission_mode": permission_mode,
            "append_system_prompt": append_system_prompt,
            "settings_file": str(settings_file) if settings_file else None,
            "worktree": worktree, "resume_session_id": resume_session_id,
            "add_dirs": [str(d) for d in add_dirs or []],
            "wo_id": wo_id, "project": project,
        }
        out = self._exec("spawn", variables, cwd=cwd, env=env)
        spec = self._spec("spawn") or {}
        if not spec.get("job_id"):
            return None
        value = extract(spec["job_id"], out)
        return str(value) if value not in (None, "") else None

    def roster(self, cwd: Path | None = None) -> list[BgSession]:
        spec = self._spec("list") or {}
        out = self._exec("list", {"cwd": str(cwd) if cwd else ""}, cwd=cwd, timeout=60)
        sessions_spec: dict[str, Any] = spec.get("sessions") or {"from": "stdout_json"}
        raw = extract(sessions_spec, out)
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise LauncherError(
                f"launcher {self.name!r}: list.sessions did not resolve to a list "
                f"(got {type(raw).__name__})"
            )
        fields = sessions_spec.get("fields") or {}
        state_map = spec.get("state_map") or {}
        sessions = []
        for item in raw:
            if not isinstance(item, dict):
                continue

            def pick(key: str) -> str:
                return str(item.get(fields.get(key, key), "") or "")

            state = pick("state") or "unknown"
            sessions.append(BgSession(
                id=pick("id"),
                session_id=pick("session_id"),
                cwd=pick("cwd"),
                name=pick("name"),
                state=state_map.get(state, state),
                kind="background",
            ))
        return sessions

    def result(self, job_id: str) -> tuple[str | None, str | None]:
        """(state, final message) for a job — best effort, like the native reader.

        Never raises: this runs on every reconcile tick for every open work order, and
        an unreadable status file is a normal transient, not an incident.
        """
        spec = self._spec("result")
        if spec is None:
            return None, None
        try:
            if spec.get("file"):
                path = Path(_substitute(spec["file"], {"job_id": job_id})).expanduser()
                data = json.loads(path.read_text())
            else:
                out = self._exec("result", {"job_id": job_id}, timeout=60)
                data = json.loads(out or "null")
        except (OSError, json.JSONDecodeError, LauncherError):
            return None, None
        state = _dig(data, (spec.get("state") or {}).get("path"))
        text = _dig(data, (spec.get("text") or {}).get("path"))
        state_map = (self._spec("list") or {}).get("state_map") or {}
        state = state_map.get(state, state) if isinstance(state, str) else None
        return state, text if isinstance(text, str) else None

    def send(self, session_id: str, message: str, cwd: Path,
             job_id: str | None = None) -> str:
        return self._exec("send", {"session_id": session_id, "message": message,
                                   "cwd": str(cwd), "job_id": job_id},
                          cwd=cwd, timeout=900).strip()

    def stop(self, job_id: str) -> bool:
        if not self.supports("stop"):
            return False
        try:
            self._exec("stop", {"job_id": job_id}, timeout=60)
            return True
        except LauncherError:
            return False

    def available(self) -> bool:
        spec = self._spec("spawn") or {}
        argv = spec.get("command") or [""]
        first = argv[0] if isinstance(argv[0], str) else ""
        return bool(first) and (shutil.which(first) is not None or Path(first).exists())


Launcher = NativeLauncher | ContractLauncher


# -- resolution ---------------------------------------------------------------------------


def contract_source(project: Any) -> Path | None:
    """Which contract file governs this project, if any (catalog > project > fleet)."""
    from .paths import jarvis_home

    candidates: list[Path] = []
    override = getattr(project, "launcher", None)
    if override:
        candidates.append(Path(override).expanduser())
    candidates.append(Path(project.path) / PROJECT_CONTRACT)
    candidates.append(jarvis_home() / FLEET_CONTRACT)
    for path in candidates:
        if path.is_file():
            return path
    if override:
        # An explicit catalog pointer that isn't there is a configuration error, not a
        # silent fall-through to the native launcher.
        raise LauncherError(
            f"project {project.name}: launcher contract {override} does not exist"
        )
    return None


def launcher_for(project: Any) -> Launcher:
    """The launcher governing a project. Falls back to native, which needs no config."""
    path = contract_source(project)
    if path is None:
        return NativeLauncher()
    return ContractLauncher(load_contract(path), source=str(path))


def sessions_under(sessions: list[BgSession], path: Path) -> list[BgSession]:
    root = str(path)
    return [s for s in sessions if s.cwd == root or s.cwd.startswith(root + "/")]


def ensure_worktree(project_path: Path, wo_id: str) -> Path:
    """Create (or reuse) the isolated worktree a launcher can't make for itself.

    Mirrors what `claude --worktree` does natively: a fresh branch off HEAD checked out
    under .claude/worktrees/<wo-id>, so a worker that can't be given one still never
    edits the user's working copy.
    """
    target = project_path / ".claude" / "worktrees" / wo_id
    if target.is_dir():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["git", "worktree", "add", "-b", f"worktree-{wo_id}", str(target)],
        cwd=project_path, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise LauncherError(
            f"could not create a worktree for {wo_id}: {proc.stderr.strip()[:300]}"
        )
    return target


# -- verification --------------------------------------------------------------------------


def verify(launcher: Launcher, project_path: Path, live: bool = False,
           timeout: float = 60.0) -> dict[str, Any]:
    """Check a launcher, statically and (opt-in) against reality.

    The static pass is free and catches the boring failures: missing binary, verbs that
    were never written. `live` is the only thing that proves a contract, so it is also
    the only thing that stamps `verified_at` — see `onboarding.record_verification`.
    """
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str = "") -> bool:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    contract = getattr(launcher, "contract", None)
    runnable = launcher.available()
    check("binary", runnable,
          "" if runnable else "the spawn command's executable is not on PATH")
    if contract is not None:
        for verb in REQUIRED_VERBS:
            check(f"verb:{verb}", launcher.supports(verb))
        for verb in ("result", "send", "stop"):
            supported = launcher.supports(verb)
            checks.append({"check": f"verb:{verb}", "ok": True,
                           "detail": "present" if supported else
                                     "absent — degraded (see the protocol doc)"})
        drift = source_drift(contract)
        check("provenance", not drift,
              f"sources changed since the contract was written: {', '.join(drift)}"
              if drift else "")
    else:
        checks.append({"check": "contract", "ok": True,
                       "detail": "no contract — using the built-in native launcher"})

    report: dict[str, Any] = {
        "launcher": launcher.name, "source": launcher.source, "live": live,
        "capabilities": launcher.capabilities.as_dict(), "checks": checks,
    }
    if contract is not None:
        report["fingerprint"] = fingerprint(contract)
    if not live:
        report["ok"] = all(c["ok"] for c in checks)
        return report

    job_id: str | None = None
    seen: BgSession | None = None
    try:
        job_id = launcher.spawn(prompt=PROBE_PROMPT, cwd=project_path, name=PROBE_NAME,
                                permission_mode="auto")
        check("spawn", True, f"job {job_id}" if job_id else "spawned (no job id reported)")
        check("job_id", job_id is not None,
              "" if job_id else "spawn reported no id — per-turn replies cannot be "
                                "captured back into the work order")
        deadline = time.time() + timeout
        while True:
            seen = next((s for s in launcher.roster(project_path)
                         if s.name == PROBE_NAME or (job_id and s.id == job_id)), None)
            if seen or time.time() >= deadline:
                break
            time.sleep(1.0)
        check("list", seen is not None,
              f"state={seen.state}" if seen else
              f"the probe session never appeared in `list` within {timeout:.0f}s")
        if seen and not (seen.is_active or seen.is_blocked or seen.is_finished):
            check("state_map", False,
                  f"state {seen.state!r} maps to nothing Jarvis understands — add it "
                  f"to list.state_map")
        if job_id and launcher.supports("result"):
            state, _text = launcher.result(job_id)
            check("result", state is not None,
                  f"state={state}" if state else "result verb returned nothing yet "
                                                 "(the probe may still be running)")
    except LauncherError as e:
        check("spawn", False, str(e))
    finally:
        # Always take the probe down: a self-test that leaves a live agent behind has
        # created exactly the mess it was meant to detect.
        stop_id = job_id or (seen.id if seen else None)
        if stop_id:
            check("stop", launcher.stop(stop_id),
                  "" if launcher.supports("stop") else "no stop verb — the probe "
                                                       "session must be ended by hand")
    report["ok"] = all(c["ok"] for c in checks)
    return report
