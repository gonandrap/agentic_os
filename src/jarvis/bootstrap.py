"""Project bootstrap ("adopt"): make a project OS-ready, idempotently"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .catalog import ProjectSpec
from .paths import project_state_dir

# v5 = v4's privileged-action gates section + the decision-routing rewrite (Neo as first responder)
TEMPLATE_VERSION = 16
ASSETS = Path(__file__).parent / "assets"


def jarvis_hook_command() -> str:
    """Absolute command for injected hooks"""
    exe = shutil.which("jarvis")
    if exe:
        return f"{exe} _hook"
    return f"{sys.executable} -m jarvis.cli _hook"


def _rebuild(src: Path, root: Path, leaf: str) -> Path:
    """Rebuild `root/.claude/<leaf>/` from `src`, wholesale"""
    dest = root / ".claude" / leaf
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest)
    return root


#: Prepended to a planning seat's definition when the project has deselected Serena
NO_SERENA_SEAT_NOTE = (
    "> **Serena is not wired for this project.** It was deselected on Jarvis's\n"
    "> /config page, so its symbol tools are not in your tool list and there is\n"
    "> nothing to activate. Read every instruction below about `activate_project`\n"
    "> and the symbol tools as already answered: Serena is genuinely unavailable\n"
    "> here, so use `Glob`, `Grep` and `Read`, and say so in your answer.\n\n"
)

_SERENA_TOOL_ENTRY = re.compile(r",\s*mcp__(?:plugin_)?serena[A-Za-z0-9_]*")


def _strip_serena(text: str) -> str:
    """A seat definition with its Serena tool grants removed and the note prepended"""
    head, sep, body = text.partition("---\n")          # opening fence
    front, sep2, rest = body.partition("\n---\n")      # closing fence
    if not sep or not sep2:
        return NO_SERENA_SEAT_NOTE + text  # not front matter we recognise; still honest
    return f"{head}{sep}{_SERENA_TOOL_ENTRY.sub('', front)}{sep2}\n{NO_SERENA_SEAT_NOTE}{rest}"


def install_agent_assets(project_path: Path, kind: str = "worker",
                         serena: bool = True) -> list[Path]:
    """Materialize the OS-provided agent assets for a work order of `kind`"""
    def _seats(src: str, leaf: str) -> Path:
        root = _rebuild(ASSETS / src, project_state_dir(project_path) / leaf, "agents")
        if not serena:
            # Safe to rewrite in place: `_rebuild` just dropped and recopied the whole destination
            for seat in (root / ".claude" / "agents").glob("*.md"):
                seat.write_text(_strip_serena(seat.read_text()))
        return root

    roots = [_rebuild(ASSETS / "skills",
                      project_state_dir(project_path) / "agent-skills", "skills")]
    if kind == "planner":
        roots.append(_seats("agents", "agent-seats"))
    if kind == "worker":
        # The crew an ordinary worker must delegate to
        roots.append(_seats("worker-agents", "agent-crew"))
    return roots


def _tree_matches(src: Path, dest: Path) -> bool:
    """Same set of files, same bytes — used to avoid rewriting an already-current skill"""
    rel = {p.relative_to(src) for p in src.rglob("*") if p.is_file()}
    if rel != {p.relative_to(dest) for p in dest.rglob("*") if p.is_file()}:
        return False
    return all((src / r).read_bytes() == (dest / r).read_bytes() for r in rel)


def install_project_skills(project_path: Path) -> list[str]:
    """Install the OS's user-facing skills into the project itself"""
    written: list[str] = []
    root = project_path / ".claude" / "skills"
    for src in sorted(p for p in (ASSETS / "project-skills").iterdir() if p.is_dir()):
        dest = root / src.name
        if dest.exists():
            if _tree_matches(src, dest):
                continue
            # No catalog knob customizes a skill, so a local edit is drift, not configuration
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest)
        written.append(src.name)
    return written


@dataclass
class BootstrapReport:
    project: str
    actions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def note(self, msg: str) -> None:
        self.actions.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursive dict merge; override wins; lists and scalars are replaced"""
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _settings_hash(settings: dict[str, Any]) -> str:
    clean = {k: v for k, v in settings.items() if k != "_jarvis"}
    return hashlib.sha256(json.dumps(clean, sort_keys=True).encode()).hexdigest()[:16]


def build_settings(overrides: dict[str, Any]) -> dict[str, Any]:
    text = (ASSETS / "settings.base.json").read_text()
    base = json.loads(text.replace("__JARVIS_HOOK_CMD__", jarvis_hook_command()))
    base.pop("$comment", None)
    merged = deep_merge(base, overrides)
    merged["_jarvis"] = {"managed": True, "version": TEMPLATE_VERSION}
    merged["_jarvis"]["hash"] = _settings_hash(merged)
    return merged


def settings_drift(settings_path: Path) -> str | None:
    """Return a drift description if the injected settings were manually edited"""
    if not settings_path.exists():
        return "missing"
    try:
        current = json.loads(settings_path.read_text())
    except json.JSONDecodeError:
        return "unparseable"
    marker = current.get("_jarvis")
    if not isinstance(marker, dict) or not marker.get("managed"):
        return "not managed by jarvis"
    if marker.get("hash") != _settings_hash(current):
        return "manually edited since injection"
    return None


def inject_settings(project: ProjectSpec, report: BootstrapReport, force: bool = False) -> None:
    claude_dir = project.path / ".claude"
    claude_dir.mkdir(exist_ok=True)
    settings_path = claude_dir / "settings.json"
    desired = build_settings(project.settings_overrides)

    if settings_path.exists():
        drift = settings_drift(settings_path)
        current_text = settings_path.read_text()
        if current_text.strip() == json.dumps(desired, indent=2).strip():
            report.note("settings.json already up to date")
            return
        if drift in ("not managed by jarvis",):
            backup = claude_dir / "settings.json.pre-jarvis"
            if not backup.exists():
                backup.write_text(current_text)
                report.note(f"backed up existing settings to {backup.name}")
        elif drift == "manually edited since injection" and not force:
            report.warn(
                "settings.json was manually edited since Jarvis injected it — "
                "re-run with --force-config to overwrite (put customizations in the "
                "catalog's settings_overrides instead)"
            )
            return
    settings_path.write_text(json.dumps(desired, indent=2) + "\n")
    report.note("injected .claude/settings.json")


def ensure_gitignore(project: ProjectSpec, report: BootstrapReport) -> None:
    gi = project.path / ".gitignore"
    line = ".jarvis/"
    text = gi.read_text() if gi.exists() else ""
    if line not in text.split("\n"):
        with gi.open("a") as f:
            if text and not text.endswith("\n"):
                f.write("\n")
            f.write(f"\n# Jarvis OS local state\n{line}\n")
        report.note("added .jarvis/ to .gitignore")


def ensure_readme(project: ProjectSpec, report: BootstrapReport) -> None:
    readme = project.path / "README.md"
    if readme.exists():
        return
    readme.write_text(
        f"# {project.name}\n\n{project.description or 'TODO: describe this project.'}\n\n"
        "<!-- Stub generated by Jarvis OS — replace with a real description. -->\n"
    )
    report.note("generated README.md stub (please edit)")
    report.warn("README.md was missing — a stub was generated, edit it")


SPECIFICS_MARKER = "## Project specifics"
DEFAULT_SPECIFICS = "_None yet._"


def operation_specifics(text: str) -> str:
    """The hand-written 'Project specifics' section of an existing OPERATION.md"""
    if SPECIFICS_MARKER not in text:
        return DEFAULT_SPECIFICS
    return text.split(SPECIFICS_MARKER, 1)[1].strip() or DEFAULT_SPECIFICS


def render_operation_md(project: ProjectSpec,
                        specifics: str = DEFAULT_SPECIFICS) -> str:
    """The contract exactly as it belongs on disk, rendered but not written"""
    return (ASSETS / "OPERATION.md.tmpl").read_text().format(
        template_version=TEMPLATE_VERSION,
        project_name=project.name,
        project_specifics=specifics,
        gates_section=_gates_section(project),
    )


def tracked_in_vcs(path: Path) -> bool:
    """Whether the repository — rather than the OS — owns this file"""
    try:
        proc = subprocess.run(
            ["git", "-C", str(path.parent), "ls-files", "--error-unmatch", "--",
             path.name],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):  # no git binary, or it hung
        return False
    return proc.returncode == 0


def _clean_in_vcs(path: Path) -> bool:
    """Whether the working-tree bytes equal HEAD's, so deleting the file loses nothing"""
    try:
        proc = subprocess.run(
            ["git", "-C", str(path.parent), "diff", "--quiet", "HEAD", "--", path.name],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def ensure_operation_md(project: ProjectSpec, report: BootstrapReport,
                        force: bool = False) -> None:
    """Generate/refresh OPERATION.md, preserving the 'Project specifics' section"""
    op_path = project.path / "OPERATION.md"
    current = op_path.read_text() if op_path.exists() else None
    desired = render_operation_md(
        project, operation_specifics(current) if current is not None else DEFAULT_SPECIFICS)

    if current == desired:
        report.note("OPERATION.md already up to date")
        return
    if current is not None and not force and tracked_in_vcs(op_path):
        was = re.search(r"template v(\d+)", current.split("\n", 1)[0])
        report.warn(
            f"OPERATION.md is committed at template v{was.group(1) if was else '?'} but "
            f"the generator renders v{TEMPLATE_VERSION} — left alone, because the OS "
            f"does not overwrite files the repository owns. Regenerate and commit it: "
            f"`jarvis adopt {project.path} --force-config`")
        return
    op_path.write_text(desired)
    report.note("wrote OPERATION.md")


def _gates_section(project: ProjectSpec) -> str:
    """The privileged-actions clause of the contract, for OPERATION.md"""
    from .gates import KINDS

    if not project.gates:
        return "\n" + "\n".join([
            "",
            "Nothing in this project ships without a human. You open pull requests;",
            "merging them, cutting releases and restarting services are the user's call.",
            "Finish the work order with the PR link and say plainly that it is ready —",
            "do not attempt the merge or the release yourself.",
        ])
    live = [k for k in KINDS if k.name in project.gates.enabled]
    lines = [
        "",
        "",
        "These actions are **gated, not forbidden**: you may attempt them, and an",
        "independent reviewer (Neo, the user's delegate) decides whether they run.",
        "",
    ]
    # With the two questions each kind's own request has to answer
    for k in live:
        lines += [
            f"- `{k.name}` — {k.summary}",
            f"  - `--why`: {k.why_ask}",
            f"  - `--evidence`: {k.evidence_ask}",
        ]
    lines += [
        "",
        "**BEFORE you are blocked** — nothing has been filed yet, so the command string is",
        "the only handle there is. Ask first: the reviewer sees ONLY the text you write, so",
        "a request with evidence is far more likely to be approved than a bare attempt.",
        "Answer the two questions listed above for the kind that fired:",
        "",
        "```bash",
        'jarvis gate request "$JARVIS_WO_ID" "<the exact command>" \\',
        '    --why "<the --why your kind asks for>" \\',
        '    --evidence "<the --evidence your kind asks for>"',
        "```",
        "",
        "**AFTER a block, use the REQUEST NUMBER the block printed — never the command",
        "string.** The OS already has the string; re-typing it into an argument is how a",
        "worker in a git worktree finds its own way out refused by the isolation guard,",
        "which inspects arguments and rejects any text it cannot prove is not a git",
        "operation. Every exit below takes the number, and none of them runs the command:",
        "",
        "```bash",
        "jarvis gate explain <request-number>                       # which exit do you need?",
        'jarvis gate request <request-number> --why "..." --evidence "..."',
        'jarvis gate contest <request-number> --why "..."',
        "```",
        "",
        "Then END YOUR TURN. The verdict arrives as your next user turn. If approved, run",
        "that exact command — the grant is scoped to that one string and expires, so do",
        "not reword it. If denied, fix what the reason names; retrying unchanged will be",
        "blocked again.",
        "",
        "There is a third verdict, **dismissed**, and its own command. The gate recognises",
        "commands by matching text, so it sometimes fires on one that merely *names* a",
        "privileged action — a release script inside a grep pattern, a path quoted in a PR",
        "body. That is a defect in the OS, not a refusal, and the request above is the wrong",
        "move for it: the command does none of these things, so both answers would be false.",
        "Contest the match instead, and nothing is authorised either way — by NUMBER, since",
        "you are reading this after a block:",
        "",
        "```bash",
        'jarvis gate contest <request-number> --why "<why this performs no privileged action>"',
        "```",
        "",
        "So when a gate fires on something you know ships nothing, do not reword the command",
        "to slip past it and do not walk away from it: contest it, and the OS learns a",
        "standing rule that spares the next worker the same block.",
        "",
        "A dismissal clears a command *string*; it does not reset the review state of the",
        "action that string talks about. So never open a second request for a privileged",
        "action while an equivalent one is still pending or escalated — a dismissal is not",
        "permission to re-file. If the first request stalled for want of evidence, send the",
        "evidence to the reviewer and leave the original request standing.",
    ]
    return "\n".join(lines)


def workspace_trusted(project_path: Path) -> bool | None:
    """Whether Claude Code trusts this workspace (None = unknown / never opened)"""
    cfg = Path(os.environ.get("JARVIS_CLAUDE_JSON", "~/.claude.json")).expanduser()
    if not cfg.exists():
        return None
    try:
        data = json.loads(cfg.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    entry = (data.get("projects") or {}).get(str(project_path))
    if entry is None:
        return None
    return bool(entry.get("hasTrustDialogAccepted"))


def _claude_json_path() -> Path:
    return Path(os.environ.get("JARVIS_CLAUDE_JSON", "~/.claude.json")).expanduser()


def ensure_trust(project: ProjectSpec, report: BootstrapReport) -> None:
    """Trust the workspace on the user's behalf, so workers never stall"""
    if workspace_trusted(project.path) is True:
        report.note("workspace already trusted")
        return
    cfg = _claude_json_path()
    try:
        data = json.loads(cfg.read_text()) if cfg.exists() else {}
    except (json.JSONDecodeError, OSError) as e:
        report.warn(f"could not read {cfg} to trust workspace ({e}); workers may stall "
                    "until the workspace is trusted")
        return
    projects = data.setdefault("projects", {})
    entry = projects.setdefault(str(project.path), {})
    entry["hasTrustDialogAccepted"] = True
    _write_claude_json(cfg, data)
    report.note("trusted workspace in ~/.claude.json (hasTrustDialogAccepted)")


def _write_claude_json(cfg: Path, data: dict[str, Any]) -> None:
    """Replace ~/.claude.json whole, so a crash never leaves it half-written"""
    tmp = cfg.with_suffix(cfg.suffix + ".jarvis-tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(cfg)


def ensure_project_skills(project: ProjectSpec, report: BootstrapReport) -> None:
    """Give the user's own sessions in this project the OS skills meant for them"""
    written = install_project_skills(project.path)
    if written:
        report.note("installed .claude/skills/: " + ", ".join(written))
    else:
        report.note(".claude/skills/ already up to date")


def ensure_state_dir(project: ProjectSpec, report: BootstrapReport) -> None:
    state = project.path / ".jarvis"
    if not state.exists():
        state.mkdir()
        report.note("created .jarvis/ state dir")


def bootstrap_project(project: ProjectSpec, force_config: bool = False,
                      dry_run: bool = False) -> BootstrapReport:
    report = BootstrapReport(project=project.name)
    if not project.path.is_dir():
        report.warn(f"path does not exist: {project.path}")
        return report
    if not (project.path / ".git").exists():
        report.warn(f"not a git repository: {project.path} — run `git init` there first")
        return report
    if dry_run:
        report.note("(dry run — no changes written)")
        readme = project.path / "README.md"
        if not readme.exists():
            report.note("would generate README.md stub")
        report.note("would write OPERATION.md, .jarvis/, .gitignore entry, settings.json, "
                    ".claude/skills/")
        if workspace_trusted(project.path) is not True:
            report.note("would trust workspace in ~/.claude.json")
        return report
    ensure_readme(project, report)
    ensure_operation_md(project, report, force=force_config)
    ensure_state_dir(project, report)
    ensure_gitignore(project, report)
    inject_settings(project, report, force=force_config)
    ensure_project_skills(project, report)
    ensure_trust(project, report)
    return report


def _empty_after(d: Path, gone: set[Path]) -> bool:
    """Whether `d` holds nothing but paths in `gone`; a kept empty child counts (issue 1330)"""
    return d.is_dir() and all(c in gone for c in d.iterdir())


def unbootstrap_project(project: ProjectSpec, *, purge: bool,
                        dry_run: bool) -> BootstrapReport:
    """Take back out what bootstrap_project wrote, never a user edit (docs/superpowers/specs/2026-10-28-offboard-a-project.md §4)"""
    report = BootstrapReport(project=project.name)
    root = project.path
    if not root.is_dir():
        report.warn(f"path does not exist: {root}")
        return report
    if dry_run:
        report.note("(dry run — no changes written)")
    gone: set[Path] = set()

    def do(verb: str, path: Path, fn: Any, *also: Path) -> None:
        label = path.relative_to(root) if path.is_relative_to(root) else path
        report.note(f"would {verb} {label}" if dry_run else f"{verb.rstrip('e')}ed {label}")
        gone.update((path, *also))
        if not dry_run:
            fn()

    claude = root / ".claude"
    settings, backup = claude / "settings.json", claude / "settings.json.pre-jarvis"
    drift = settings_drift(settings)
    if drift in ("missing", "not managed by jarvis"):
        report.note("Jarvis .claude/settings.json already absent")
    elif drift is not None:
        report.warn(f".claude/settings.json left in place: {drift}")
    elif backup.exists():
        do("restore", settings, lambda: backup.replace(settings), backup)
        gone.discard(settings)
    else:
        do("remove", settings, settings.unlink)

    skills = claude / "skills"
    for src in sorted(p for p in (ASSETS / "project-skills").iterdir() if p.is_dir()):
        dest = skills / src.name
        if not dest.exists():
            report.note(f".claude/skills/{src.name} already absent")
        elif _tree_matches(src, dest):
            do("remove", dest, lambda d=dest: shutil.rmtree(d))
        else:
            report.warn(f".claude/skills/{src.name} left in place: edited since Jarvis installed it")
    for d in (skills, claude):
        if d.exists() and _empty_after(d, gone):
            do("remove", d, d.rmdir)

    state, gi = root / ".jarvis", root / ".gitignore"
    op = root / "OPERATION.md"
    if not op.exists():
        report.note("OPERATION.md already absent")
    else:
        tracked = tracked_in_vcs(op)
        if _clean_in_vcs(op) if tracked else op.read_text() == render_operation_md(project):
            do("remove", op, op.unlink)
        elif not purge:
            keep = state / "OPERATION.md.offboarded"
            def stash() -> None:
                state.mkdir(exist_ok=True)
                op.replace(keep)
            do("move", op, stash)
            report.note(f"  to {keep.relative_to(root)}, keeping its edits")
        else:
            report.warn("OPERATION.md left in place: it holds edits version control cannot restore; "
                        f"delete it yourself: rm {op}")
        if tracked and op in gone:
            report.warn("OPERATION.md is committed; its deletion is left for you to commit: "
                        "git rm --cached OPERATION.md && git commit")

    cfg = _claude_json_path()
    try:
        data = json.loads(cfg.read_text()) if cfg.exists() else {}
    except (json.JSONDecodeError, OSError) as e:
        data = None
        report.warn(f"could not read {cfg} to remove workspace trust ({e}); left in place")
    if data is not None:
        entry = (data.get("projects") or {}).get(str(root))
        if not isinstance(entry, dict) or "hasTrustDialogAccepted" not in entry:
            report.note("workspace trust already absent")
        else:
            def untrust() -> None:
                del entry["hasTrustDialogAccepted"]
                _write_claude_json(cfg, data)
            do("remove", cfg, untrust)
            gone.discard(cfg)

    if not purge:
        report.note("kept .jarvis/ and its .gitignore line (archive keeps history)")
        return report
    if state.exists():
        do("remove", state, lambda: shutil.rmtree(state))
    else:
        report.note(".jarvis/ already absent")
    lines = gi.read_text().split("\n") if gi.exists() else []
    if ".jarvis/" not in lines:
        report.note(".jarvis/ line in .gitignore already absent")
    else:
        end = lines.index(".jarvis/")
        start = end
        if start and lines[start - 1] == "# Jarvis OS local state":
            start -= 1
            if start and not lines[start - 1]:
                start -= 1  # the blank line ensure_gitignore wrote before its block
        kept = "\n".join(lines[:start] + lines[end + 1:])
        do("edit", gi, lambda: gi.write_text(kept))
        gone.discard(gi)
    return report
