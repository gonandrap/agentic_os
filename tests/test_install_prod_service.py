"""`scripts/install_prod_service.sh` — the script that decides what the daemon can run.

This file exists because of a production incident, and it is worth stating the incident
plainly: on 2026-08-10 both `wo-67d4f8b0` and `wo-996c7344` stayed parked in
`waiting_pr_merge` after their pull requests had merged. The merge-poll logic was fine —
`tests/test_wo_pr_merge.py` covers it in 26 tests — but the daemon could not run `gh` at
all, so none of that logic ever executed. `gh` on the host is a snap at `/snap/bin/gh`;
the runtime PATH this script renders into the unit probes exactly `uv`, `claude` and
`node` and then appends a fixed list that has no `/snap/bin` in it. `github.gh_bin()`
fell back to the bare string `gh`, `subprocess` raised `FileNotFoundError`, and
pull-request polling was silently off for every project (issue #90).

Why nothing caught it: every test in `test_wo_pr_merge.py` injects a fake `gh`, so binary
*resolution* — the one thing that actually broke — is stubbed out in all of them, and
this script had no tests whatsoever. It still has no `--dry-run`: it runs `systemctl
daemon-reload`, `enable` and `restart` unconditionally, so a test that drove it top to
bottom would restart the real production daemon. That is the deeper reason the coverage
gap existed, and it is why these tests execute the runtime-PATH block on its own, in a
sandbox, rather than running the script whole. The block is sliced out of the real file,
never copied into this one — a copy would keep passing after the original changed.

The companion suite for the *other* installer is `tests/test_install_script.py`, which
already guards this exact class of bug for `install.sh`
(`test_warns_when_the_bin_dir_is_not_on_path`, "a silent off-PATH install is a trap").
The production units never got the same treatment.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

#: Resolved once, from the ambient PATH, because the sandbox PATH the block runs under is
#: deliberately too small to find an interpreter — that is the point of it.
BASH = shutil.which("bash") or "/bin/bash"

#: External commands the sliced block itself needs (`command -v` is a shell builtin;
#: `dirname` is not). They are symlinked into a directory that holds nothing else, so the
#: probe can never discover them and they never reach the rendered PATH.
BLOCK_NEEDS = ("dirname",)

REPO_ROOT = Path(__file__).resolve().parents[1]
PROD_SCRIPT = REPO_ROOT / "scripts" / "install_prod_service.sh"
TEMPLATES = sorted((REPO_ROOT / "deploy").glob("*.service.template"))

#: The runtime-PATH block is sliced out of the script between these two markers. They are
#: comments in the real file, and `_path_block` fails loudly if either goes missing, so
#: restructuring the script breaks these tests instead of quietly voiding them.
BLOCK_START = "# Runtime PATH:"
BLOCK_END = "# UI port from the prod catalog"

#: Every external binary a daemon-spawned process invokes *by name*, and what needs it.
#: `gh` is the one this file was written for. It is not decoration: the OS shells out to
#: it from `github.pr_view` (auto-complete on merge) and from `bugreport.create_issue`
#: (`jarvis bug report`, which every worker has as a skill), and both were dead in
#: production while it was unresolvable.
DAEMON_BINARIES = {
    "uv": "workers run `uv run pytest`",
    "claude": "the worker process itself — `claude -p …`",
    "node": "the Claude CLI's own runtime",
    "gh": "github.pr_view (auto-complete on merge) and bugreport.create_issue",
}


def _path_block() -> str:
    """The real script's runtime-PATH construction, sliced out so it can be run alone."""
    lines = PROD_SCRIPT.read_text().splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.startswith(BLOCK_START)]
    ends = [i for i, ln in enumerate(lines) if ln.startswith(BLOCK_END)]
    assert starts, f"{PROD_SCRIPT.name} no longer has a {BLOCK_START!r} marker"
    assert ends, f"{PROD_SCRIPT.name} no longer has a {BLOCK_END!r} marker"
    block = "\n".join(lines[starts[0]:ends[0]])
    assert "RUNTIME_PATH" in block, "the sliced block does not build RUNTIME_PATH"
    return block


def _fake_bin(directory: Path, name: str) -> Path:
    """An executable that `command -v` will find. Contents are irrelevant — the script
    only ever asks where it is."""
    directory.mkdir(parents=True, exist_ok=True)
    exe = directory / name
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    return exe


def _support_dir(tmp_path: Path) -> Path:
    """Somewhere for the block's own dependencies, holding nothing the probe looks for."""
    support = tmp_path / "_support"
    support.mkdir(parents=True, exist_ok=True)
    for name in BLOCK_NEEDS:
        real = shutil.which(name)
        assert real, f"the test host has no `{name}`"
        link = support / name
        if not link.exists():
            link.symlink_to(real)
    return support


def _render_path(tmp_path: Path, *, lookup_dirs: list[Path]) -> list[str]:
    """Run the real block with a controlled PATH and HOME; return the rendered entries.

    The sandbox PATH contains only what the test planted, so a binary that happens to be
    installed on the host running pytest can never make one of these assertions pass.
    Nothing outside `tmp_path` is touched and no `systemctl` runs: this is the block on
    its own, which is the only part of the script that is safe to execute in a test.
    """
    prod_dir = tmp_path / "production" / "jarvis_os"
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    script = "\n".join([
        "set -euo pipefail",
        f"PROD_DIR={shlex.quote(str(prod_dir))}",
        _path_block(),
        'printf "%s" "$RUNTIME_PATH"',
    ])
    sandbox = [*lookup_dirs, _support_dir(tmp_path)]
    proc = subprocess.run(
        [BASH, "-c", script], capture_output=True, text=True,
        env={"HOME": str(home), "PATH": ":".join(str(d) for d in sandbox)},
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.split(":")


# -- the script itself ---------------------------------------------------------------


def test_syntax_is_valid_bash():
    proc = subprocess.run(["bash", "-n", str(PROD_SCRIPT)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_is_executable():
    assert os.access(PROD_SCRIPT, os.X_OK), "install_prod_service.sh must be chmod +x"


# -- the runtime PATH ----------------------------------------------------------------


def test_the_prod_venv_comes_first(tmp_path):
    """`jarvis` must resolve to the production install, not to a dev checkout that
    happens to be earlier on the ambient PATH."""
    entries = _render_path(tmp_path, lookup_dirs=[])
    assert entries[0] == str(tmp_path / "production" / "jarvis_os" / ".venv" / "bin")


def test_a_probed_binary_puts_its_directory_on_the_path(tmp_path):
    """The discovery mechanism: wherever a needed binary actually lives, that directory
    must reach the unit. This is what makes the install portable across hosts."""
    odd = tmp_path / "opt" / "somewhere" / "bin"
    _fake_bin(odd, "uv")
    assert str(odd) in _render_path(tmp_path, lookup_dirs=[odd])


def test_the_path_never_repeats_a_directory(tmp_path):
    """Two probed binaries in one directory must not list it twice — `add()` dedupes."""
    shared = tmp_path / "opt" / "bin"
    _fake_bin(shared, "uv")
    _fake_bin(shared, "claude")
    entries = _render_path(tmp_path, lookup_dirs=[shared])
    assert entries.count(str(shared)) == 1
    assert len(entries) == len(set(entries)), f"duplicate entries in {entries}"


def test_the_base_directories_are_always_present(tmp_path):
    """The fixed tail, which is what a host with nothing else installed falls back to."""
    entries = _render_path(tmp_path, lookup_dirs=[])
    for base in ("/usr/local/bin", "/usr/bin", "/bin"):
        assert base in entries
    assert str(tmp_path / "home" / ".local" / "bin") in entries


def test_a_binary_that_is_nowhere_is_skipped_without_failing(tmp_path):
    """`set -e` plus a failing `command -v` is an easy way to abort an installer; the
    script must render a usable PATH on a host that is missing a tool entirely."""
    entries = _render_path(tmp_path, lookup_dirs=[])
    assert entries and all(entries), f"empty entry in {entries}"


@pytest.mark.xfail(
    strict=False,
    reason="issue #90: the probe list omits `gh`, so a snap-installed gh never reaches "
           "the unit and PR-merge polling is dead in production. The fix is owned by "
           "wo-238b9372 — when it lands this XPASSes and the marker should be deleted.",
)
def test_every_binary_the_daemon_shells_out_to_is_on_the_service_path(tmp_path):
    """The regression test for the incident this file documents.

    Each binary is planted in its own directory that is on nobody's fixed list — the
    point is that the script must *discover* where a tool lives, exactly as it already
    does for `uv`, `claude` and `node`. `gh` is the one that fails today: a host with
    `gh` installed as a snap renders a unit that cannot run it, and every work order
    that finishes behind a pull request then parks for ever.
    """
    dirs = {}
    for name in DAEMON_BINARIES:
        directory = tmp_path / "elsewhere" / name / "bin"
        _fake_bin(directory, name)
        dirs[name] = directory

    entries = _render_path(tmp_path, lookup_dirs=list(dirs.values()))

    missing = {n: f"{dirs[n]} absent — needed by {why}"
               for n, why in DAEMON_BINARIES.items() if str(dirs[n]) not in entries}
    assert not missing, (
        "the rendered service PATH cannot resolve: " + "; ".join(sorted(missing.values()))
    )


# -- the units the script renders ----------------------------------------------------


def test_there_are_templates_to_render():
    assert TEMPLATES, "deploy/*.service.template disappeared"


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.name)
def test_every_placeholder_in_a_template_is_substituted(template):
    """A placeholder with no matching `sed` expression ships to systemd verbatim, and
    `Environment=PATH=@PATH@` is a unit that starts with a broken PATH rather than one
    that fails loudly."""
    script = PROD_SCRIPT.read_text()
    placeholders = sorted(set(re.findall(r"@[A-Z_]+@", template.read_text())))
    assert placeholders, f"{template.name} has no placeholders — is it still a template?"
    unsubstituted = [p for p in placeholders if f"s#{p}#" not in script]
    assert not unsubstituted, (
        f"{template.name} uses {unsubstituted} but install_prod_service.sh never "
        f"substitutes them"
    )


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.name)
def test_every_unit_carries_the_rendered_path(template):
    """The link between this script and the incident: the runtime PATH is the *only*
    PATH these processes get. systemd does not inherit the login shell's one, which is
    why a `gh` that works in a terminal can be unreachable to the daemon."""
    assert "Environment=PATH=@PATH@" in template.read_text()
