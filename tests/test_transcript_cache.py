"""Parsed transcripts reused until the file changes (docs/superpowers/specs/2026-10-29-the-cost-page-reuses-what-it-parsed.md)"""

from __future__ import annotations

import json

import pytest

from jarvis import transcript_cache, usage

from test_usage import row


@pytest.fixture()
def transcript(tmp_path, monkeypatch):
    """One transcript file, with a helper that appends rows to it"""
    root = tmp_path / "projects" / "-proj"
    root.mkdir(parents=True)
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(root.parent))
    path = root / "s1.jsonl"
    path.write_text("")

    def append(*rows: dict) -> None:
        with path.open("a") as handle:
            for item in rows:
                handle.write(json.dumps(item) + "\n")

    append(row("m1", write=100, out=10))
    return path, append


def test_a_second_read_of_an_unchanged_transcript_does_not_parse_it_again(
        transcript, monkeypatch):
    path, _ = transcript
    calls = []
    real = usage._read_assistant_messages
    monkeypatch.setattr(usage, "_read_assistant_messages",
                        lambda p: (calls.append(p), real(p))[1])

    first = usage._assistant_messages(path)
    second = usage._assistant_messages(path)

    assert len(calls) == 1
    assert first == second
    assert first[0]["output_tokens"] == 10


def test_an_appended_transcript_is_parsed_again_and_the_new_rows_show_up(transcript):
    path, append = transcript
    assert len(usage._assistant_messages(path)) == 1

    append(row("m2", write=50, out=7))

    messages = usage._assistant_messages(path)
    assert [m["output_tokens"] for m in messages] == [10, 7]


def test_each_product_of_one_transcript_is_cached_apart(transcript):
    path, _ = transcript
    usage._assistant_messages(path)
    assert transcript_cache.stats() == {"files": 1, "products": 1}

    usage.compactions_in(path)
    usage.tool_results(path)

    assert transcript_cache.stats() == {"files": 1, "products": 3}


def test_tool_results_are_rebuilt_so_a_caller_cannot_poison_the_cache(transcript):
    path, _ = transcript
    first = usage.tool_results(path)
    first.clear()

    assert usage.tool_results(path) == usage.tool_results(path)


def test_a_different_chars_per_token_is_a_different_product(transcript):
    path, _ = transcript
    usage.tool_results(path)
    usage.tool_results(path, chars_per_token=2.0)

    assert transcript_cache.stats() == {"files": 1, "products": 2}


def test_what_was_saved_is_what_loads_back(transcript):
    path, _ = transcript
    before = usage._assistant_messages(path)
    transcript_cache.save()

    transcript_cache.reset()
    assert transcript_cache.stats() == {"files": 0, "products": 0}

    assert usage._assistant_messages(path) == before
    assert transcript_cache.stats() == {"files": 1, "products": 1}


def test_a_transcript_that_changed_while_the_cache_was_saved_is_parsed_again(
        transcript):
    path, append = transcript
    usage._assistant_messages(path)
    transcript_cache.save()
    append(row("m2", out=7))
    transcript_cache.reset()

    assert [m["output_tokens"] for m in usage._assistant_messages(path)] == [10, 7]


def test_a_damaged_cache_file_is_ignored_rather_than_raised(transcript):
    path, _ = transcript
    transcript_cache.cache_path().parent.mkdir(parents=True, exist_ok=True)
    transcript_cache.cache_path().write_bytes(b"not gzip at all")
    transcript_cache.reset()

    assert len(usage._assistant_messages(path)) == 1


def test_the_cache_is_bounded_by_file_count(tmp_path, monkeypatch):
    root = tmp_path / "projects" / "-proj"
    root.mkdir(parents=True)
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(root.parent))
    monkeypatch.setattr(transcript_cache, "MAX_FILES", 3)
    for n in range(6):
        path = root / f"s{n}.jsonl"
        path.write_text(json.dumps(row(f"m{n}", out=n)) + "\n")
        usage._assistant_messages(path)

    assert transcript_cache.stats()["files"] == 3


def test_a_transcript_that_cannot_be_stat_ed_is_still_read(tmp_path):
    missing = tmp_path / "gone.jsonl"

    assert usage._assistant_messages(missing) == []
    assert transcript_cache.stats() == {"files": 0, "products": 0}
