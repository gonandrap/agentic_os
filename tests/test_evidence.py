"""The evidence packet and the round fingerprint.

Every test here is a PAIR. "The fingerprint did not move" is worthless on its own — a
function that returns a constant passes it — so each inert case is asserted beside an
identically-shaped case that is NOT inert, in the same test body. The same discipline
applies to truncation ("cut" beside "not cut") and to the worktree ("gone" beside
"present").

Nothing here fakes git. The repositories are real, built with `make_git_project` plus the
`_git` subprocess helper the bug-report and shipit suites already use, because the whole
module is a reading of what git actually says and a fake would only test the fake.
"""

from __future__ import annotations

import ast
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from jarvis import evidence
from jarvis.testing import make_git_project

WO_FIELDS = {"id": "wo-1", "title": "Title", "description": "The brief",
             "result_summary": "the summary", "pr_url": "", "worktree": "wt"}


def _git(cwd: Path, *args: str) -> str:
    """Run git with a pinned identity and no user or system config in sight."""
    env = {"HOME": str(cwd), "PATH": os.environ.get("PATH", ""),
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, env=env,
                          capture_output=True, text=True).stdout


def _body(name: str, n: int, salt: str = "") -> str:
    return "".join(f"{name} line {i} {salt}\n" for i in range(n))


@dataclass
class Env:
    """A project repo and one worker worktree cut from it.

    The default branch is `trunk`, and `main` exists beside it pointing at the same
    commit. That is deliberate: it is what makes the top two rungs of the merge-base
    ladder distinguishable. If the remote's default were also `main`, rung 1 and rung 2
    would both answer "origin/main" and neither test would prove anything.
    """

    project: Path
    worktree: Path

    def collect(self, *, declared: str = "ran the tests", **over) -> evidence.EvidencePacket:
        wo = {**WO_FIELDS, **over}
        chars = wo.pop("diff_chars", evidence.DEFAULT_DIFF_CHARS)
        return evidence.collect_work_order(self.project, wo, declared=declared,
                                           diff_chars=chars)


@pytest.fixture()
def env(tmp_path) -> Env:
    project = make_git_project(tmp_path, "proj")
    _git(project, "symbolic-ref", "HEAD", "refs/heads/trunk")
    (project / "app.py").write_text(_body("app", 30))
    (project / "lib.py").write_text(_body("lib", 30))
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "base")
    _git(project, "branch", "main")
    _git(tmp_path, "init", "--bare", "-q", str(tmp_path / "origin.git"))
    _git(project, "remote", "add", "origin", str(tmp_path / "origin.git"))
    _git(project, "push", "-q", "origin", "trunk", "main")
    _git(project, "remote", "set-head", "origin", "trunk")
    worktree = project / ".claude" / "worktrees" / "wt"
    _git(project, "worktree", "add", "-q", "-b", "wo-branch", str(worktree), "trunk")
    return Env(project, worktree)


def _headers(diff: str) -> dict[str, str]:
    """Split a unified diff into {path: section text}, independently of the module."""
    out: dict[str, str] = {}
    path, buf = "", []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if buf:
                out[path] = "".join(buf)
            rest = line[len("diff --git "):].rstrip("\n")
            path = rest[rest.rfind(" b/") + 3:]
            buf = [line]
        else:
            buf.append(line)
    if buf:
        out[path] = "".join(buf)
    return out


# ------------------------------------------------------------------- the fingerprint


def test_an_empty_commit_does_not_move_the_fingerprint_but_a_byte_does(env):
    """`head` is not in the hash; the diff is. Both halves in one test, because
    "equal" and "a constant" are the same observation without the second half."""
    (env.worktree / "app.py").write_text(_body("app", 30, "edited"))
    _git(env.worktree, "commit", "-aqm", "real work")
    before = env.collect()

    _git(env.worktree, "commit", "-q", "--allow-empty", "-m", "nothing at all")
    after = env.collect()
    assert after.head != before.head           # the pairing is real: HEAD moved
    assert evidence.fingerprint(after) == evidence.fingerprint(before)

    (env.worktree / "app.py").write_text(_body("app", 30, "edited") + "x\n")
    assert evidence.fingerprint(env.collect()) != evidence.fingerprint(before)


def test_summary_pr_url_and_base_are_not_in_the_fingerprint(env):
    """Three collects of one worktree differing only in those, then one that differs
    in the diff."""
    (env.worktree / "app.py").write_text(_body("app", 30, "edited"))
    plain = env.collect()
    reworded = env.collect(result_summary="a completely different summary",
                           pr_url="https://github.com/o/r/pull/9")

    # A different rung of the ladder, resolving to the same commit: `base` moves and
    # not one byte of the diff does.
    _git(env.project, "symbolic-ref", "-d", "refs/remotes/origin/HEAD")
    rebased = env.collect()
    assert (plain.base, rebased.base) == ("origin/trunk", "origin/main")
    assert plain.summary != reworded.summary and plain.pr_url != reworded.pr_url

    prints = {evidence.fingerprint(p) for p in (plain, reworded, rebased)}
    assert len(prints) == 1

    (env.worktree / "lib.py").write_text(_body("lib", 30, "also edited"))
    assert evidence.fingerprint(env.collect()) not in prints


def test_reflowed_declared_text_fingerprints_identically_but_a_changed_word_does_not(env):
    (env.worktree / "app.py").write_text(_body("app", 30, "edited"))
    tidy = env.collect(declared="uv run pytest -q: 412 passed, 0 failed")
    reflowed = env.collect(declared="   uv run pytest -q:\n\t412 passed,\n     0 failed  ")
    assert evidence.fingerprint(reflowed) == evidence.fingerprint(tidy)

    changed = env.collect(declared="uv run pytest -q: 412 passed, 1 failed")
    assert evidence.fingerprint(changed) != evidence.fingerprint(tidy)


def test_the_same_worktree_fingerprints_identically_at_two_truncation_limits(env):
    """The sharpest test in the file. Hashing `packet.diff` — the obvious implementation
    — fails exactly here, and would make an integrity check depend on a display setting."""
    for i in range(6):
        (env.worktree / f"f{i}.py").write_text(_body(f"f{i}", 60))
    _git(env.worktree, "add", "-A")

    whole = env.collect(diff_chars=60000)
    cut = env.collect(diff_chars=200)
    assert whole.diff_truncated is False and cut.diff_truncated is True  # not vacuous
    assert len(cut.diff) < len(whole.diff)
    assert evidence.fingerprint(cut) == evidence.fingerprint(whole)


# --------------------------------------------------------------------- the truncation


def test_truncation_reports_what_it_dropped_and_never_touches_the_file_list(env):
    for i in range(12):
        (env.worktree / f"f{i:02d}.py").write_text(_body(f"f{i}", 60))
    _git(env.worktree, "add", "-A")
    independent = [n for n in _git(env.worktree, "diff", "--name-only", "HEAD").split("\n") if n]

    cut = env.collect(diff_chars=2000)
    assert cut.diff_truncated is True
    assert cut.dropped_files != ()
    assert set(cut.dropped_files) <= set(cut.files)   # dropped names STAY in `files`
    assert len(cut.files) == len(independent) == 12
    assert len(cut.diff) <= 2000

    whole = env.collect(diff_chars=60000)
    assert (whole.diff_truncated, whole.dropped_files) == (False, ())
    assert whole.files == cut.files


def test_the_file_list_survives_a_limit_that_keeps_no_diff_at_all(env):
    """`files` is what lets a seat say "you claim tests were added and no path under
    tests/ is here" even when the patch itself was cut to nothing."""
    for i in range(40):
        (env.worktree / f"f{i:02d}.py").write_text(_body(f"f{i}", 20))
    _git(env.worktree, "add", "-A")

    cut = env.collect(diff_chars=1)
    assert len(cut.files) == 40
    assert len(cut.dropped_files) == 40
    assert cut.diff == ""


def test_every_kept_file_is_byte_identical_to_its_own_git_diff(env):
    """Stronger than counting `@@` markers: the body under each header is compared with
    what git prints for that single path, so a cut inside a hunk cannot pass."""
    for i in range(12):
        (env.worktree / f"f{i:02d}.py").write_text(_body(f"f{i}", 60))
    _git(env.worktree, "add", "-A")

    cut = env.collect(diff_chars=4000)
    sections = _headers(cut.diff)
    assert sections and cut.diff_truncated is True
    for path, text in sections.items():
        assert text == _git(env.worktree, "diff", "HEAD", "--", path)


def test_a_changed_binary_file_does_not_break_the_boundary_rule(env):
    """A binary diff carries no hunk at all, so an implementation that cuts on `@@`
    mangles it and no corpus of text diffs would ever show that."""
    png = env.worktree / "a_img.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 2)
    _git(env.worktree, "add", "-A")
    _git(env.worktree, "commit", "-qm", "add an image")
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 3)
    for i in range(6):
        (env.worktree / f"z{i}.py").write_text(_body(f"z{i}", 60))
    _git(env.worktree, "add", "-A")

    # The image appears twice: added in the committed half, modified in the working
    # tree. Both sections are binary, and both must survive whole.
    added = _git(env.worktree, "diff", "origin/trunk...HEAD", "--", "a_img.png")
    modified = _git(env.worktree, "diff", "HEAD", "--", "a_img.png")
    assert "Binary files" in added and "Binary files" in modified

    # Room for the image and nothing else: it sorts first, so it is the file kept.
    cut = env.collect(diff_chars=len(added) + len(modified) + 10)
    assert cut.diff_truncated is True
    assert cut.diff == added + modified
    assert "a_img.png" not in cut.dropped_files
    assert set(cut.dropped_files) == {f"z{i}.py" for i in range(6)}


# --------------------------------------------------------------------- the merge base


def test_rung_1_the_remote_says_what_its_default_branch_is(env):
    (env.worktree / "app.py").write_text(_body("app", 30, "edited"))
    packet = env.collect()
    assert packet.base == "origin/trunk"
    assert packet.files == ("app.py",)


def test_rung_2_origin_main_when_the_remote_head_symref_is_gone(env):
    _git(env.project, "symbolic-ref", "-d", "refs/remotes/origin/HEAD")
    (env.worktree / "app.py").write_text(_body("app", 30, "edited"))
    packet = env.collect()
    assert packet.base == "origin/main"
    assert packet.files == ("app.py",)


def test_rung_3_the_local_main_when_there_is_no_remote(env):
    _git(env.project, "symbolic-ref", "-d", "refs/remotes/origin/HEAD")
    _git(env.project, "remote", "remove", "origin")
    (env.worktree / "app.py").write_text(_body("app", 30, "edited"))
    packet = env.collect()
    assert packet.base == "main"
    assert packet.files == ("app.py",)


def test_rung_4_no_base_at_all_still_sees_uncommitted_work(env):
    _git(env.project, "symbolic-ref", "-d", "refs/remotes/origin/HEAD")
    _git(env.project, "remote", "remove", "origin")
    _git(env.project, "branch", "-D", "main")

    (env.worktree / "app.py").write_text(_body("app", 30, "edited"))
    dirty = env.collect()
    assert dirty.base == ""
    assert dirty.head != ""
    assert dirty.files == ("app.py",)

    _git(env.worktree, "checkout", "--", "app.py")
    clean = env.collect()
    assert (clean.base, clean.files, clean.diff) == ("", (), "")


def test_committed_and_uncommitted_work_both_reach_the_diff(env):
    """The half most likely to be dropped, and its absence is invisible in every test
    that remembers to commit."""
    (env.worktree / "app.py").write_text(_body("app", 30, "committed"))
    _git(env.worktree, "commit", "-aqm", "committed half")
    (env.worktree / "lib.py").write_text(_body("lib", 30, "still dirty"))

    packet = env.collect()
    assert packet.base == "origin/trunk"
    assert set(packet.files) == {"app.py", "lib.py"}
    assert "committed" in packet.diff and "still dirty" in packet.diff
    assert "app.py" in packet.stat and "lib.py" in packet.stat


# ------------------------------------------------------- the worktree and the branch


def test_a_missing_worktree_yields_an_empty_packet_and_no_exception(env):
    (env.worktree / "app.py").write_text(_body("app", 30, "edited"))
    present = env.collect()
    assert present.files != () and present.head != ""

    gone = env.collect(worktree="never-existed")
    assert gone.files == ()
    assert (gone.diff, gone.base, gone.head, gone.stat) == ("", "", "", "")
    assert gone.diff_truncated is False and gone.dropped_files == ()
    assert gone.title == present.title      # the brief still travels


def test_a_missing_worktree_never_falls_back_to_the_project_root(env):
    """Collecting the user's own checkout as "the worker's evidence" is a silent lie,
    and it is the failure this test exists for."""
    (env.project / "app.py").write_text(_body("app", 30, "the user's own edit"))
    assert _git(env.project, "diff", "--name-only", "HEAD").strip() == "app.py"  # dirty

    gone = env.collect(worktree="never-existed")
    assert gone.files == ()
    assert "the user's own edit" not in gone.diff


def test_the_branch_column_is_not_read(env):
    """`work_orders.branch` is declared and written by nothing, so it is always NULL —
    and it looks exactly like the column you want. Behavioural, so it survives a
    refactor that a source grep would not notice."""
    (env.worktree / "app.py").write_text(_body("app", 30, "edited"))
    null = env.collect(branch=None)
    nonsense = env.collect(branch="no-such-branch")
    assert null == nonsense
    assert null.files == ("app.py",)


# -------------------------------------------------------------------- import surface


def _imports(path: Path) -> set[str]:
    """Every module a file imports, function bodies included."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            found.update([base] if node.module else [base + a.name for a in node.names])
    return found


def test_the_module_imports_nothing_that_could_have_seen_the_work():
    """Walks the AST, function bodies included: the house style is a lazy import inside
    the function that needs it, and a `sys.modules` check would miss every one."""
    found = _imports(Path(evidence.__file__))
    assert found == {"__future__", "hashlib", "subprocess", "dataclasses", "pathlib",
                     "typing", ".worker_session"}
    for forbidden in (".catalog", ".daemon", ".bus", ".neo", ".panel", ".claude_cli",
                      ".neo_store", ".ops", ".project_store"):
        assert forbidden not in found


def test_only_the_round_machine_collects_evidence():
    """The packet has exactly two callers, and they are the two halves of one round.

    `ops` collects it when a submission opens a round; `daemon` collects it again on its
    own thread when that round is judged. Anything else appearing here is a module that
    has started forming its own opinion about what a work order changed — which is the
    coupling this leaf exists to avoid — so the list is asserted whole rather than
    "nothing imports it".
    """
    src = Path(evidence.__file__).parent
    names = {".evidence", "jarvis.evidence"}
    importers = [p.name for p in sorted(src.glob("*.py"))
                 if p.name != "evidence.py" and _imports(p) & names]
    assert importers == ["daemon.py", "ops.py"]
