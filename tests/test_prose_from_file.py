"""Prose from a file: the worktree guard refuses argv text whose first word is a binary

Issue 1209. `jarvis gate explain "git status"` is refused from a worktree-isolated
session before jarvis runs, and so is any question or learning whose text STARTS with
that word; the same word mid-sentence passes. So every prose slot a worker fills has a
`--<slot>-file PATH` sibling, and the text never enters argv.
"""

from __future__ import annotations

import io

import pytest

from jarvis import cli, ops
from jarvis.central_store import CentralStore

# The payload the guard will not carry in argv
REFUSED = "git worktrees are what the guard reads this argument as"

ARGV = [
    (["wo", "ask", "wo-1", "--question-file"], "question"),
    (["wo", "assume", "wo-1", "--content-file"], "content"),
    (["wo", "send", "wo-1", "--message-file"], "message"),
    (["wo", "finish", "wo-1", "--summary-file"], "summary"),
    (["wo", "review", "wo-1", "--feedback-file"], "feedback"),
    (["gate", "request", "wo-1", "ship it", "--why-file"], "why"),
    (["gate", "request", "wo-1", "ship it", "--evidence-file"], "evidence"),
    (["learn", "add", "--content-file"], "content"),
]


def _file(tmp_path, text=REFUSED):
    path = tmp_path / "prose.txt"
    path.write_text(text)
    return str(path)


def _parse(argv):
    return cli.build_parser().parse_args(argv)


@pytest.mark.parametrize(("argv", "slot"), ARGV, ids=[s + "-" + " ".join(a[:2])
                                                      for a, s in ARGV])
def test_every_prose_slot_can_be_filled_from_a_file(argv, slot, tmp_path):
    args = _parse([*argv, _file(tmp_path)])

    cli.fill_prose_from_files(args)

    assert getattr(args, slot) == REFUSED


@pytest.mark.parametrize(("argv", "slot"), ARGV, ids=[s + "-" + " ".join(a[:2])
                                                      for a, s in ARGV])
def test_a_dash_reads_the_slot_from_stdin(argv, slot, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(REFUSED + "\n"))
    args = _parse([*argv, "-"])

    cli.fill_prose_from_files(args)

    assert getattr(args, slot) == REFUSED


def test_both_spellings_at_once_is_refused(tmp_path):
    args = _parse(["wo", "ask", "wo-1", "a question", "--question-file",
                   _file(tmp_path)])

    with pytest.raises(ops.OpsError, match="given twice"):
        cli.fill_prose_from_files(args)


def test_neither_spelling_is_refused():
    """`question` is nargs='?' now, so the requirement moved here"""
    args = _parse(["wo", "ask", "wo-1"])

    with pytest.raises(ops.OpsError, match="--question-file"):
        cli.fill_prose_from_files(args)


def test_only_one_slot_can_read_stdin(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(REFUSED))
    args = _parse(["gate", "request", "wo-1", "ship it", "--why-file", "-",
                   "--evidence-file", "-"])

    with pytest.raises(ops.OpsError, match="stdin"):
        cli.fill_prose_from_files(args)


def test_a_missing_file_is_refused(tmp_path):
    args = _parse(["wo", "ask", "wo-1", "--question-file", str(tmp_path / "nope.txt")])

    with pytest.raises(ops.OpsError, match="no such question file"):
        cli.fill_prose_from_files(args)


def test_an_empty_file_is_refused(tmp_path):
    args = _parse(["wo", "ask", "wo-1", "--question-file", _file(tmp_path, "  \n")])

    with pytest.raises(ops.OpsError, match="empty"):
        cli.fill_prose_from_files(args)


def test_the_summary_cap_still_applies_to_a_summary_read_from_a_file(tmp_path,
                                                                    monkeypatch):
    """The hook that caps `--summary` sees argv only, so the file path re-checks it"""
    monkeypatch.setenv("JARVIS_WO_ID", "wo-1")
    args = _parse(["wo", "finish", "wo-1", "--summary-file",
                   _file(tmp_path, "word " * 200)])

    with pytest.raises(ops.OpsError, match="the cap is"):
        cli.fill_prose_from_files(args)


def test_learn_add_writes_an_entry_whose_text_argv_would_have_refused(jarvis_home,
                                                                     tmp_path):
    assert cli.main(["learn", "add", "--content-file", _file(tmp_path)]) == 0

    central = CentralStore()
    try:
        rows = central.search_knowledge("worktrees")
    finally:
        central.close()
    assert [r for r in rows if r["content"] == REFUSED]
