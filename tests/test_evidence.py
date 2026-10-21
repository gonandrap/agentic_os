"""The evidence packet and the round fingerprint"""

from __future__ import annotations

import ast
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from jarvis import evidence
from jarvis.testing import make_git_project

WO_FIELDS = {"id": "wo-1", "title": "Title", "description": "The brief",
             "result_summary": "the summary", "pr_url": "", "worktree": "wt"}


def _git(cwd: Path, *args: str) -> str:
    """Run git with a pinned identity and no user or system config in sight"""
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
    """A project repo and one worker worktree cut from it"""

    project: Path
    worktree: Path

    def collect(self, *, declared: str = "ran the tests", assumptions=(), history=(),
                **over) -> evidence.EvidencePacket:
        wo = {**WO_FIELDS, **over}
        chars = wo.pop("diff_chars", evidence.DEFAULT_DIFF_CHARS)
        return evidence.collect_work_order(self.project, wo, declared=declared,
                                           diff_chars=chars, assumptions=assumptions,
                                           history=history)


@pytest.fixture()
def env(tmp_path) -> Env:
    # No fixture commit: this suite builds `main` itself, one rung at a time
    project = make_git_project(tmp_path, "proj", commit_spec=False)
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
    """Split a unified diff into {path: section text}, independently of the module"""
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
    """`head` is not in the hash; the diff is"""
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
    """Three collects of one worktree differing only in those, then one that differs in the diff"""
    (env.worktree / "app.py").write_text(_body("app", 30, "edited"))
    plain = env.collect()
    reworded = env.collect(result_summary="a completely different summary",
                           pr_url="https://github.com/o/r/pull/9")

    # A different rung of the ladder
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


def test_an_assumption_moves_the_fingerprint_and_the_users_verdict_on_it_does_not(env):
    """A call the submitter declared is material the judge reads"""
    (env.worktree / "app.py").write_text(_body("app", 30, "edited"))
    bare = env.collect()
    assert evidence.fingerprint(env.collect(assumptions=())) == \
        evidence.fingerprint(bare)

    filed = env.collect(assumptions=[{"n": 1, "content": "the exporter writes UTF-8",
                                      "status": "pending"}])
    assert evidence.fingerprint(filed) != evidence.fingerprint(bare)

    decided = env.collect(assumptions=[{"n": 1, "content": "the exporter writes UTF-8",
                                        "status": "accepted"}])
    assert evidence.fingerprint(decided) == evidence.fingerprint(filed)


def test_prior_round_history_is_not_in_the_fingerprint_and_the_declared_evidence_is(env):
    """The pin the whole field turns on"""
    (env.worktree / "app.py").write_text(_body("app", 30, "edited"))
    bare = env.collect()
    carried = env.collect(history=[
        {"round": 1, "outcome": "rejected", "reason": "no test covers the change",
         "head_sha": "a" * 40,
         "blockers": [{"title": "untested branch",
                       "detail": "the new branch has no test"}]}])
    assert carried.history and not bare.history          # the pairing is real
    assert evidence.fingerprint(carried) == evidence.fingerprint(bare)

    restated = env.collect(declared="ran the tests, and one of them failed")
    assert evidence.fingerprint(restated) != evidence.fingerprint(bare)


def test_history_reaches_the_packet_as_blockers_with_no_seat_and_no_severity(env):
    """The collector normalises, so no caller decides the shape"""
    (env.worktree / "app.py").write_text(_body("app", 30, "edited"))
    packet = env.collect(history=[
        {"round": 1, "outcome": "rejected", "reason": "why",
         "blockers": [{"severity": "blocker", "seat": "tester", "title": "t",
                       "detail": "d"}]}])

    entry = packet.history[0]
    assert set(entry) == {"round", "outcome", "reason", "head_sha", "blockers"}
    assert entry["head_sha"] == ""                       # absent, not guessed
    assert entry["blockers"] == ({"title": "t", "detail": "d"},)


def test_the_same_worktree_fingerprints_identically_at_two_truncation_limits(env):
    """The sharpest test in the file"""
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
    """`files` is what lets a seat say "you claim tests were added and no path under tests/ is here" even..."""
    for i in range(40):
        (env.worktree / f"f{i:02d}.py").write_text(_body(f"f{i}", 20))
    _git(env.worktree, "add", "-A")

    cut = env.collect(diff_chars=1)
    assert len(cut.files) == 40
    assert len(cut.dropped_files) == 40
    assert cut.diff == ""


def test_every_kept_file_is_byte_identical_to_its_own_git_diff(env):
    """Stronger than counting `@@` markers: the body under each header is compared with what git prints..."""
    for i in range(12):
        (env.worktree / f"f{i:02d}.py").write_text(_body(f"f{i}", 60))
    _git(env.worktree, "add", "-A")

    cut = env.collect(diff_chars=4000)
    sections = _headers(cut.diff)
    assert sections and cut.diff_truncated is True
    for path, text in sections.items():
        assert text == _git(env.worktree, "diff", "HEAD", "--", path)


def test_a_changed_binary_file_does_not_break_the_boundary_rule(env):
    """A binary diff carries no hunk at all"""
    png = env.worktree / "a_img.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 2)
    _git(env.worktree, "add", "-A")
    _git(env.worktree, "commit", "-qm", "add an image")
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 3)
    for i in range(6):
        (env.worktree / f"z{i}.py").write_text(_body(f"z{i}", 60))
    _git(env.worktree, "add", "-A")

    # The image appears twice: added in the committed half, modified in the working tree
    added = _git(env.worktree, "diff", "origin/trunk...HEAD", "--", "a_img.png")
    modified = _git(env.worktree, "diff", "HEAD", "--", "a_img.png")
    assert "Binary files" in added and "Binary files" in modified

    # Room for the image and nothing else: it sorts first, so it is the file kept
    cut = env.collect(diff_chars=len(added) + len(modified) + 10)
    assert cut.diff_truncated is True
    assert cut.diff == added + modified
    assert "a_img.png" not in cut.dropped_files
    assert set(cut.dropped_files) == {f"z{i}.py" for i in range(6)}


# ------------------------------------------------------------------ the panel's trim

HUNK_MARKER = re.compile(r"\[(\d+) more hunk\(s\) of ([^\]]+) not shown\]")


def test_the_panel_trim_still_drops_whole_files_at_its_own_budget(env):
    """Invariant 1: `_truncate` is unchanged"""
    for i in range(12):
        (env.worktree / f"f{i:02d}.py").write_text(_body(f"f{i}", 60))
    _git(env.worktree, "add", "-A")

    cut = env.collect(diff_chars=4000)
    assert cut.diff_truncated and cut.dropped_files
    assert not HUNK_MARKER.search(cut.diff)
    for path, text in _headers(cut.diff).items():
        assert text == _git(env.worktree, "diff", "HEAD", "--", path)


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
    """The half most likely to be dropped"""
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
    """Collecting the user's own checkout as "the worker's evidence" is a silent lie"""
    (env.project / "app.py").write_text(_body("app", 30, "the user's own edit"))
    assert _git(env.project, "diff", "--name-only", "HEAD").strip() == "app.py"  # dirty

    gone = env.collect(worktree="never-existed")
    assert gone.files == ()
    assert "the user's own edit" not in gone.diff


def test_the_branch_column_is_not_read(env):
    """`work_orders.branch` is declared and written by nothing, so it is always NULL"""
    (env.worktree / "app.py").write_text(_body("app", 30, "edited"))
    null = env.collect(branch=None)
    nonsense = env.collect(branch="no-such-branch")
    assert null == nonsense
    assert null.files == ("app.py",)


# -------------------------------------------------------------------- import surface


def _imports(path: Path) -> set[str]:
    """Every module a file imports, function bodies included"""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            found.update([base] if node.module else [base + a.name for a in node.names])
    return found


def test_the_module_imports_nothing_that_could_have_seen_the_work():
    """Walks the AST, function bodies included: the house style is a lazy import inside the function that..."""
    found = _imports(Path(evidence.__file__))
    assert found == {"__future__", "hashlib", "logging", "subprocess", "dataclasses",
                     "pathlib", "typing", ".worker_session", ".github"}
    for forbidden in (".catalog", ".daemon", ".bus", ".neo", ".panel", ".claude_cli",
                      ".neo_store", ".ops", ".project_store"):
        assert forbidden not in found


def test_only_the_round_machine_collects_evidence():
    """The packet has exactly three readers: the two halves of one round (docs/superpowers/specs/2026-10-14-neo-never-reads-a-diff.md)"""
    src = Path(evidence.__file__).parent
    names = {".evidence", "jarvis.evidence"}
    importers = [p.name for p in sorted(src.glob("*.py"))
                 if p.name != "evidence.py" and _imports(p) & names]
    assert importers == ["daemon.py", "landing.py", "ops.py", "validation.py"]
    assert "collect_work_order" not in (src / "landing.py").read_text()
    # `autoreview` is `landing`'s case: it TRIMS a packet the daemon collected
    assert "collect_work_order" not in (src / "autoreview.py").read_text()


# ------------------------------------------------- the per-file digest map (file_shas)


def test_two_files_get_different_digests_and_only_the_edited_one_moves(env):
    """The map's whole job: `validation.unanswered_submission` asks what MOVED between two rounds"""
    (env.worktree / "app.py").write_text(_body("app", 30, "edited"))
    (env.worktree / "lib.py").write_text(_body("lib", 30, "edited"))
    _git(env.worktree, "commit", "-aqm", "both")
    first = dict(env.collect().file_shas)
    assert set(first) == {"app.py", "lib.py"}
    assert first["app.py"] != first["lib.py"]

    (env.worktree / "app.py").write_text(_body("app", 30, "edited twice"))
    _git(env.worktree, "commit", "-aqm", "one of them")
    second = dict(env.collect().file_shas)
    assert second["app.py"] != first["app.py"]
    assert second["lib.py"] == first["lib.py"]


def test_a_rename_is_keyed_on_the_new_path_and_the_map_matches_files(env):
    """The docstring's claim, and what makes the map usable beside `files`: a citation is compared against..."""
    (env.worktree / "pkg").mkdir()
    _git(env.worktree, "mv", "lib.py", "pkg/lib.py")
    _git(env.worktree, "commit", "-qm", "moved")
    packet = env.collect()
    shas = dict(packet.file_shas)
    assert "pkg/lib.py" in shas
    assert set(shas) == set(packet.files)


def test_the_map_covers_a_file_whose_patch_was_DROPPED_by_truncation(env):
    """`file_digests` runs before `_truncate`"""
    (env.worktree / "app.py").write_text(_body("app", 30, "edited"))
    (env.worktree / "lib.py").write_text(_body("lib", 30, "edited"))
    _git(env.worktree, "commit", "-aqm", "both")

    whole = dict(env.collect().file_shas)
    packet = env.collect(diff_chars=900)
    assert packet.diff_truncated and packet.dropped_files
    shas = dict(packet.file_shas)
    for path in packet.dropped_files:
        assert path not in packet.diff, "a dropped file is not in the kept text"
        assert shas.get(path), f"{path} lost its digest to truncation"
    # PRESENT is not enough: a map built from the kept text could still key every path and give the...
    assert shas == whole
    assert set(shas) == set(packet.files)

    # ...and the digest of a DROPPED file still moves when that file does
    dropped = packet.dropped_files[0]
    (env.worktree / dropped).write_text(_body(dropped.split(".")[0], 30, "again"))
    _git(env.worktree, "commit", "-aqm", "the dropped one")
    after = env.collect(diff_chars=900)
    assert dropped in after.dropped_files and dropped not in after.diff
    assert dict(after.file_shas)[dropped] != shas[dropped]


# -- which commit a round judged


def _packet(**over) -> evidence.EvidencePacket:
    base = dict(unit="work_order", subject_id="wo-1", title="t", description="d",
                summary="s", declared="ran the tests", pr_url="", base="main",
                head="abc1234", stat="", files=(), diff="", diff_truncated=False,
                dropped_files=(), diff_sha="")
    return evidence.EvidencePacket(**{**base, **over})


def test_judged_head_answers_for_a_feature_and_still_fails_closed_for_a_work_order():
    """§5(a) of... (docs/superpowers/specs/2026-10-07-a-feature-round-must-judge-a-head-that-contains-its-children.md)"""
    assert evidence.judged_head(_packet(unit="feature", head="f" * 40)) == "f" * 40
    assert evidence.judged_head(_packet(unit="feature", head="")) == ""
    assert evidence.judged_head(_packet()) == ""
    assert evidence.judged_head(
        _packet(source="pull_request", head="a-branch",
                pr={"head_sha": "a" * 40})) == "a" * 40
