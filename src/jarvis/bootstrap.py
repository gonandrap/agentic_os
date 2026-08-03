"""Project bootstrap ("adopt"): make a project OS-ready, idempotently.

- README.md         required; stub generated when missing
- OPERATION.md      the operating contract, generated from a versioned template
- .jarvis/          per-project state dir, added to .gitignore
- .claude/settings.json  injected: OS baseline deep-merged with catalog overrides,
                    marked with a "_jarvis" key (managed flag + content hash) so
                    manual drift is detected on the next start
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .catalog import ProjectSpec
from .paths import project_state_dir

# v5 = v4's privileged-action gates section + the decision-routing rewrite (Neo as
# first responder). Both landed as "v4" on separate branches, so a project that
# regenerated against either one would have skipped the other's content.
# v6 = the `dismissed` gate verdict. A worker that does not know a false positive has an
# outcome other than "denied" reasonably concludes the command is forbidden and starts
# rewording it to get past the recogniser, which is the one response that must not be
# learned. Bumping the version is what pushes that paragraph into every managed repo.
# v7 = the `[<wo-id>] ` pull request title rule, and `jarvis wo finish --pr`. Numbered 7
# rather than 6 because the `dismissed` paragraph above ALSO landed as v6, on another
# branch — exactly the collision the v5 note describes, caught this time in a merge
# instead of by a project that silently skipped one branch's content.
TEMPLATE_VERSION = 7
ASSETS = Path(__file__).parent / "assets"


def jarvis_hook_command() -> str:
    """Absolute command for injected hooks — the Claude daemon's PATH may not
    include wherever jarvis is installed."""
    exe = shutil.which("jarvis")
    if exe:
        return f"{exe} _hook"
    return f"{sys.executable} -m jarvis.cli _hook"


def _rebuild(src: Path, root: Path, leaf: str) -> Path:
    """Rebuild `root/.claude/<leaf>/` from `src`, wholesale. Returns `root`.

    Generated, never authored: the whole destination is dropped and recopied on each
    dispatch, so a stale or locally mangled copy heals itself.
    """
    dest = root / ".claude" / leaf
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest)
    return root


def install_agent_assets(project_path: Path, kind: str = "worker") -> list[Path]:
    """Materialize the OS-provided agent assets for a work order of `kind`; return the
    directories to hand Claude via `--add-dir`.

    Getting an asset in front of a worker is awkward: workers run in a fresh git
    worktree, so an untracked `.claude/` in the main checkout never reaches them, and no
    settings key can declare an extra skills directory (checked against the CLI). What
    does work — verified live, with a negative control — is `--add-dir X`, which loads
    skills from `X/.claude/skills/` and subagent definitions from `X/.claude/agents/`.
    So the OS keeps both inside the project's gitignored `.jarvis/` tree and points each
    worker at whichever it is entitled to on spawn.

    **Two roots, not one, and the split is load-bearing.** Skills go to every worker;
    the planning seats go to planners only (the design's decision 4 — an ordinary worker
    is an individual with one job and gets no profile). Putting the seats in the skills
    root and omitting them for workers would mean the worker path had to DELETE
    `agents/` to keep owning its whole generated tree — and that delete would land while
    a concurrently-dispatched planner turn was reading it. Separate roots make the two
    populations independent, so neither dispatch can disturb the other.
    """
    roots = [_rebuild(ASSETS / "skills",
                      project_state_dir(project_path) / "agent-skills", "skills")]
    if kind == "planner":
        roots.append(_rebuild(ASSETS / "agents",
                              project_state_dir(project_path) / "agent-seats", "agents"))
    return roots


def _tree_matches(src: Path, dest: Path) -> bool:
    """Same set of files, same bytes — used to avoid rewriting an already-current skill."""
    rel = {p.relative_to(src) for p in src.rglob("*") if p.is_file()}
    if rel != {p.relative_to(dest) for p in dest.rglob("*") if p.is_file()}:
        return False
    return all((src / r).read_bytes() == (dest / r).read_bytes() for r in rel)


def install_project_skills(project_path: Path) -> list[str]:
    """Install the OS's user-facing skills into the project itself; return the names
    written (empty when everything was already current).

    Different audience from `install_agent_assets`, so a different delivery. Those are
    for the workers Jarvis dispatches and ride in on `--add-dir` at spawn time. These are
    for the sessions the *user* opens by hand, which get no such flag and only ever load
    `<project>/.claude/skills/` — so they are written into the project, next to the
    `.claude/settings.json` bootstrap already injects there. Like that file they are
    untracked and not gitignored: the OS leaves it to the project whether to commit them.

    Only the subdirectories the OS owns are touched. `<project>/.claude/skills/` is where
    users keep their own skills, so this never clears the tree — contrast
    `install_agent_assets`, which owns its whole (gitignored, generated) destination and
    rebuilds it wholesale.
    """
    written: list[str] = []
    root = project_path / ".claude" / "skills"
    for src in sorted(p for p in (ASSETS / "project-skills").iterdir() if p.is_dir()):
        dest = root / src.name
        if dest.exists():
            if _tree_matches(src, dest):
                continue
            # No catalog knob customizes a skill, so a local edit is drift, not
            # configuration — preserving it would mean the OS could never ship a fix.
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
    """Recursive dict merge; override wins; lists and scalars are replaced."""
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
    """Return a drift description if the injected settings were manually edited."""
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


def ensure_operation_md(project: ProjectSpec, report: BootstrapReport) -> None:
    """Generate/refresh OPERATION.md, preserving the 'Project specifics' section."""
    op_path = project.path / "OPERATION.md"
    specifics = "_None yet._"
    if op_path.exists():
        current = op_path.read_text()
        if f"template v{TEMPLATE_VERSION}" in current.split("\n", 1)[0]:
            report.note("OPERATION.md already at current template version")
            return
        marker = "## Project specifics"
        if marker in current:
            specifics = current.split(marker, 1)[1].strip() or specifics
    template = (ASSETS / "OPERATION.md.tmpl").read_text()
    op_path.write_text(
        template.format(
            template_version=TEMPLATE_VERSION,
            project_name=project.name,
            project_specifics=specifics,
            gates_section=_gates_section(project),
        )
    )
    report.note("wrote OPERATION.md")


def _gates_section(project: ProjectSpec) -> str:
    """The privileged-actions clause of the contract, for OPERATION.md.

    Written from the catalog rather than hard-coded because the answer differs per
    project, and getting it wrong in either direction is costly: a worker that wrongly
    believes it may ship will try, and a worker that wrongly believes it may not will
    stop short and leave the release to a human who was never told it was ready.
    """
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
    lines += [f"- `{k.name}` — {k.summary}" for k in live]
    lines += [
        "",
        "Ask before acting — the reviewer sees ONLY the text you write, so a request with",
        "evidence is far more likely to be approved than a bare attempt:",
        "",
        "```bash",
        'jarvis gate request "$JARVIS_WO_ID" "<the exact command>" \\',
        '    --why "<why this is ready to ship>" \\',
        '    --evidence "<PR number, test results, checks>"',
        "```",
        "",
        "Then END YOUR TURN. The verdict arrives as your next user turn. If approved, run",
        "that exact command — the grant is scoped to that one string and expires, so do",
        "not reword it. If denied, fix what the reason names; retrying unchanged will be",
        "blocked again.",
        "",
        "There is a third verdict, **dismissed**. The gate recognises commands by matching",
        "text, so it sometimes fires on one that merely *names* a privileged action — a",
        "release script inside a grep pattern, a path quoted in a PR body. That is a defect",
        "in the OS, not a refusal: the reviewer dismisses it, nothing is authorised, and the",
        "command goes through unchanged. So when a gate fires on something you know ships",
        "nothing, do not reword the command to slip past it. File the request, say plainly",
        "why it performs no privileged action, and end your turn.",
    ]
    return "\n".join(lines)


def workspace_trusted(project_path: Path) -> bool | None:
    """Whether Claude Code trusts this workspace (None = unknown / never opened).

    Untrusted workspaces IGNORE permissions.allow entries (verified live), which
    stalls unattended workers on their first tool call.
    """
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
    """Trust the workspace on the user's behalf, so workers never stall.

    Every project in the catalog is one the user already runs Claude in, so being in
    the catalog *is* the trust decision — we don't make them accept a dialog per
    project. Untrusted workspaces silently ignore `permissions.allow` (verified live),
    which stalls unattended workers on their first tool call. We set
    `hasTrustDialogAccepted: true` for the project path in ~/.claude.json, preserving
    every other key and writing atomically.
    """
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
    tmp = cfg.with_suffix(cfg.suffix + ".jarvis-tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(cfg)
    report.note("trusted workspace in ~/.claude.json (hasTrustDialogAccepted)")


def ensure_project_skills(project: ProjectSpec, report: BootstrapReport) -> None:
    """Give the user's own sessions in this project the OS skills meant for them."""
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
    ensure_operation_md(project, report)
    ensure_state_dir(project, report)
    ensure_gitignore(project, report)
    inject_settings(project, report, force=force_config)
    ensure_project_skills(project, report)
    ensure_trust(project, report)
    return report
