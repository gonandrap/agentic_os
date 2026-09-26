"""`jarvis watch` — what a turn is doing RIGHT NOW, and what it refuses to claim.

`tests/test_inspection.py` covers the post-mortem: a whole transcript, cut into turns,
read once. This covers the opposite regime — the same file read every two seconds while
it is still being appended to — and almost everything below is about the two ways that
regime can lie.

The first is the reader: a JSONL being written is read mid-line, so the cursor tests
exist because dropping or double-counting the newest row drops exactly the row the user
is waiting for. The second is the payload: a row lands when a MESSAGE completes, so an
open tool span, a token count and a turn clock are all stale readings between messages,
and the snapshot has to say which state it is in before any of them may be read as
current. `state`, `now is None` and `note` carry that, and they are pinned here.

The row builders are copied from `tests/test_inspection.py` rather than imported: they
describe the shape Claude Code writes, and §4 of the feature order rewrites that module's
walk. A shared fixture would make one child's edit the other's failure.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from jarvis import cli, inspection, live, ops, usage
from jarvis.project_store import ProjectStore


def stamp(at: float) -> str:
    return datetime.fromtimestamp(at, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def prompt_row(at: float, text: str, *, sdk: bool = True, meta: bool = False) -> dict:
    row = {"type": "user", "timestamp": stamp(at), "message": {"content": text}}
    if sdk:
        row["promptSource"] = "sdk"
    if meta:
        row["isMeta"] = True
    return row


def assistant_row(at: float, mid: str, *, write: int = 0, read: int = 0,
                  content: list | None = None, input: int = 0, out: int = 1) -> dict:
    counts: dict = {"input_tokens": input, "cache_creation_input_tokens": write,
                    "cache_read_input_tokens": read, "output_tokens": out}
    return {
        "type": "assistant", "timestamp": stamp(at),
        "message": {"id": mid, "model": "claude-opus-5", "usage": counts,
                    "content": content or [{"type": "text", "text": "ok"}]},
    }


def tool_rows(start: float, end: float, tool_id: str, name: str,
              payload: dict | None = None) -> list[dict]:
    return [tool_use_row(start, tool_id, name, payload),
            {"type": "user", "timestamp": stamp(end),
             "message": {"content": [{"type": "tool_result",
                                      "tool_use_id": tool_id}]}}]


def tool_use_row(at: float, tool_id: str, name: str,
                 payload: dict | None = None) -> dict:
    """The half of `tool_rows` a LIVE turn has written: the ask, with no result yet."""
    return {"type": "assistant", "timestamp": stamp(at),
            "message": {"id": f"m-{tool_id}", "model": "claude-opus-5",
                        "usage": {"input_tokens": 0, "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": 0, "output_tokens": 1},
                        "content": [{"type": "tool_use", "id": tool_id, "name": name,
                                     "input": payload or {}}]}}


@pytest.fixture()
def write_transcript(tmp_path, monkeypatch):
    """A transcript under a fake `$JARVIS_TRANSCRIPT_ROOT`, writable a row at a time.

    `append` and `append_raw` are the additions to `test_inspection.py`'s fixture and
    they are the whole point of this file: a live transcript grows between two reads, and
    `append_raw` can leave it without a trailing newline the way a half-written row does.
    """
    root = tmp_path / "projects"
    (root / "-proj").mkdir(parents=True)
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(root))

    class Fixture:
        def __init__(self, root: Path) -> None:
            self.root = root

        def path(self, session_id: str, slug: str = "-proj") -> Path:
            return self.root / slug / f"{session_id}.jsonl"

        def write(self, session_id: str, rows: list[dict], *,
                  slug: str = "-proj") -> str:
            path = self.path(session_id, slug)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("".join(json.dumps(r) + "\n" for r in rows))
            return session_id

        def append(self, session_id: str, rows: list[dict], *,
                   slug: str = "-proj") -> None:
            self.append_raw(session_id,
                            "".join(json.dumps(r) + "\n" for r in rows), slug=slug)

        def append_raw(self, session_id: str, text: str, *,
                       slug: str = "-proj") -> None:
            with self.path(session_id, slug).open("a") as f:
                f.write(text)

        def subagent_meta(self, session_id: str, name: str, meta: dict, *,
                          slug: str = "-proj") -> None:
            directory = self.root / slug / session_id / "subagents"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"{name}.meta.json").write_text(json.dumps(meta))

    return Fixture(root)


def snap(session: str, **kwargs):
    """One frame over a bound reader, with the record's two booleans defaulted off."""
    reader = live.Reader()
    reader.bind(session)
    return reader, frame(reader, **kwargs)


def frame(reader, *, wo_id: str = "wo-1111", project: str = "proj_a",
          turn_in_flight: bool = False, settled: bool = False, now: float = 2000.0,
          holds=(), write_floor: int = 20_000) -> dict:
    return reader.snapshot(wo_id=wo_id, project=project,
                           turn_in_flight=turn_in_flight, settled=settled, now=now,
                           holds=holds, write_floor=write_floor).as_dict()


DISPATCH = "You are the worker agent for wo-1111"


# -- the five states, and the reader that feeds them -----------------------------------


def test_the_states_are_the_five_the_spec_names():
    """Pinned because every renderer branches on this string and the dashboard's
    template cannot be type-checked: a sixth state added without a word for it renders
    as a blank panel, and a renamed one renders the wrong panel."""
    assert live.STATES == ("working", "generating", "idle", "settled", "no-transcript")


def test_a_second_poll_reads_only_the_bytes_that_arrived(write_transcript):
    """The reason this module exists rather than a call to `usage.rows`: at a 2s refresh
    a whole-file re-parse is paid for again on every frame, and the file only grows."""
    session = write_transcript.write("grow", [prompt_row(1000, DISPATCH),
                                             assistant_row(1010, "m1")])
    size = write_transcript.path(session).stat().st_size
    reader = live.Reader()
    reader.bind(session)

    first = frame(reader, turn_in_flight=True)
    assert reader.bytes_last_read == size
    assert first["turn"]["seq"] == 1

    write_transcript.append(session, tool_rows(1020, 1025, "t1", "Bash",
                                               {"command": "pytest -q"}))
    added = write_transcript.path(session).stat().st_size - size

    second = frame(reader, turn_in_flight=True)

    assert reader.bytes_last_read == added  # not the whole file a second time
    assert second["turn"]["seq"] == 1  # the accumulated turn survived the poll
    assert [s["tool"] for s in second["recent"]] == ["Bash"]


def test_a_replaced_file_is_re_read_from_zero(write_transcript):
    """`uilog._fingerprint`'s reasoning, and it is not theoretical here: Claude Code
    rewrites a transcript on compaction. A bare byte offset would seek past the real
    rows of the new file into whatever happened to be at that position."""
    session = write_transcript.write("swap", [prompt_row(1000, DISPATCH),
                                             assistant_row(1010, "m1")])
    reader = live.Reader()
    reader.bind(session)
    frame(reader)

    write_transcript.write(session, [prompt_row(1500, DISPATCH + " again"),
                                     assistant_row(1510, "m9"),
                                     assistant_row(1520, "m10")])
    size = write_transcript.path(session).stat().st_size

    after = frame(reader, turn_in_flight=True)

    assert reader.bytes_last_read == size  # the whole new file, from zero
    assert after["turn"]["started"] == 1500.0
    assert after["turn"]["triggers"][0]["quote"].endswith("again")


def test_a_half_written_row_is_not_parsed_until_its_newline_arrives(write_transcript):
    """THE DEFECT THIS TEST EXISTS FOR: a transcript is appended to while it is read, so
    the last line can be half there. Parsing it drops the row (invalid JSON) and
    advancing past it drops it for ever — and it is the newest row, the one the user
    opened `jarvis watch` to see."""
    session = write_transcript.write("partial", [prompt_row(1000, DISPATCH),
                                                assistant_row(1010, "m1")])
    reader = live.Reader()
    reader.bind(session)
    frame(reader)
    settled_offset = reader.offset

    half = json.dumps(tool_use_row(1020, "t1", "Bash", {"command": "pytest -q"}))
    write_transcript.append_raw(session, half[:len(half) // 2])
    mid = frame(reader, turn_in_flight=True)

    assert reader.offset == settled_offset  # the cursor did not pass the partial line
    assert mid["now"] is None

    write_transcript.append_raw(session, half[len(half) // 2:] + "\n")
    done = frame(reader, turn_in_flight=True, now=1030.0)

    assert done["now"]["tool"] == "Bash"
    assert done["state"] == "working"


def test_a_transcript_with_only_a_half_written_line_has_no_stale_reading(
        write_transcript):
    """`_last_ts` is 0.0 when nothing has parsed yet, and `now - 0.0` is 1.7 billion
    seconds of silence — a number every layer above would render as an eternally stalled
    turn. The file exists (`found` stays true) and the note says the honest thing: nothing
    has been written to it at all, not "600 seconds ago"."""
    session = write_transcript.write("half-only", [])
    half = json.dumps(prompt_row(1000, DISPATCH))
    write_transcript.append_raw(session, half[:len(half) // 2])

    _, payload = snap(session, turn_in_flight=True, now=1_700_000_000.0)

    assert payload["found"] is True
    assert payload["stale_seconds"] is None
    assert "nothing has been written to this transcript at all" in payload["note"]


# -- what the state means, and what may be read as current -----------------------------


def test_an_open_span_with_a_turn_in_flight_is_working(write_transcript):
    """The only state in which `now` is a CURRENT reading, and both halves of the test
    are load-bearing: the transcript says a tool was asked for and never came back, and
    the OS's own record says a process is running to come back with it."""
    session = write_transcript.write("busy", [
        prompt_row(1000, DISPATCH),
        assistant_row(1010, "m1"),
        tool_use_row(1015, "t1", "Bash", {"command": "uv run pytest -q"}),
    ])

    _, payload = snap(session, turn_in_flight=True, now=1075.0)

    assert payload["state"] == "working"
    assert payload["now"]["tool"] == "Bash"
    assert payload["now"]["elapsed"] == 60.0
    assert payload["now"]["params"] == {"command": "uv run pytest -q"}
    assert payload["turn"]["elapsed"] == 75.0


def test_an_open_span_with_no_turn_in_flight_reports_nothing_current(write_transcript):
    """Neo q678 (2): a killed turn leaves a `tool_use` with no `tool_result` in the file
    FOR EVER. Reading the transcript alone, "Bash has been running for 3 days" is what
    that looks like, and it is the exact shape of lie §3 forbids."""
    session = write_transcript.write("crashed", [
        prompt_row(1000, DISPATCH),
        assistant_row(1010, "m1"),
        tool_use_row(1015, "t1", "Bash", {"command": "uv run pytest -q"}),
    ])

    _, payload = snap(session, turn_in_flight=False, now=1075.0)

    assert payload["state"] == "idle"
    assert payload["now"] is None


def test_generating_names_the_time_nothing_has_been_written_since(write_transcript):
    """THE BLIND SPOT, in words. A row lands when a message completes, so a model that
    has been generating for ten minutes has written nothing — and the honest report is
    the silence and its length, never invented activity."""
    session = write_transcript.write("thinking", [prompt_row(1000, DISPATCH),
                                                 assistant_row(1010, "m1")])

    _, payload = snap(session, turn_in_flight=True, now=1610.0)

    assert payload["state"] == "generating"
    assert payload["stale_seconds"] == 600.0
    assert payload["now"] is None
    assert "nothing has been written since" in payload["note"]
    assert live.clock(1010.0) in payload["note"]


def test_no_transcript_is_absent_and_never_zero(write_transcript):
    """Issue #227's rule at a second site: a reading that could not be taken is null, so
    no layer above can average it, sum it or render it as a fast, cheap, idle turn."""
    _, payload = snap("never-ran")

    assert payload["state"] == "no-transcript" and payload["found"] is False
    for key in ("turn", "now", "tokens", "last_write", "stale_seconds"):
        assert payload[key] is None, key
    assert "no transcript" in payload["note"]


def test_a_settled_order_reports_its_last_frame_and_nothing_current(write_transcript):
    """§3: a user who runs this a minute late gets an answer rather than an error. The
    last frame is still true about the past; `now` is not, which is why `settled` is
    decided before `working` (Neo q678)."""
    session = write_transcript.write("done", [
        prompt_row(1000, DISPATCH),
        assistant_row(1010, "m1"),
        *tool_rows(1015, 1025, "t1", "Bash", {"command": "git push"}),
        tool_use_row(1030, "t2", "Read", {"file_path": "/tmp/x"}),
    ])

    _, payload = snap(session, settled=True, turn_in_flight=True, now=9000.0)

    assert payload["state"] == "settled"
    assert payload["now"] is None
    assert [s["tool"] for s in payload["recent"]] == ["Bash"]


# -- the numbers, and whose vocabulary they are in -------------------------------------


def test_the_token_counters_are_this_turn_s_not_the_session_s(write_transcript):
    """`jarvis cost` owns the session; the live view's subject is the turn in flight. A
    session total would answer a question nobody watching a turn is asking, and would
    make the counters climb while the current turn did nothing."""
    session = write_transcript.write("tokens", [
        prompt_row(1000, DISPATCH),
        assistant_row(1010, "m1", write=500_000, read=1_000, input=7, out=9),
        prompt_row(2000, "[Neo, answering for the user] yes"),
        assistant_row(2010, "m2", write=10, read=40_000, input=3, out=5),
        assistant_row(2020, "m3", write=20, read=50_000, input=1, out=11),
    ])

    _, payload = snap(session, turn_in_flight=True, now=2030.0)

    assert payload["tokens"] == {"input": 4, "output": 16, "cache_write": 30,
                                "cache_read": 90_000, "context": 50_021}


def test_last_write_carries_inspection_s_own_cause_and_raises_nothing(write_transcript):
    """One vocabulary for one fact. `jarvis inspect` already names the four causes and
    writes the sentence for each; a second wording here is the drift this repository has
    shipped before. And it REPORTS: prefix stability's authority is
    `invariants.check_prefix_stable`, so nothing in this payload is an alarm."""
    session = write_transcript.write("rewrite", [
        prompt_row(1000, DISPATCH),
        assistant_row(1010, "m1", write=300_000),
        assistant_row(1022, "m2", write=250_000),
    ])

    _, payload = snap(session, turn_in_flight=True, now=1030.0, write_floor=100_000)

    assert payload["last_write"]["cause"] == inspection.PREFIX_MISS
    assert payload["last_write"]["note"] == \
        inspection.WRITE_CAUSE_NOTES[inspection.PREFIX_MISS]
    assert payload["last_write"]["written"] == 250_000
    assert not [k for k in payload if "alarm" in k]


def test_subagents_are_named_from_the_meta_files_beside_the_transcript(write_transcript):
    """The join that turns `a7b62083` into a sentence — and no subagent transcript is
    opened: §8 owns those, and reading one per frame is the cost this module refuses."""
    session = write_transcript.write("lead", [prompt_row(1000, DISPATCH),
                                             assistant_row(1010, "m1")])
    write_transcript.subagent_meta(session, "agent-a7b62083",
                                   {"agentType": "Explore",
                                    "description": "find the dispatch path"})

    _, payload = snap(session, turn_in_flight=True)

    assert payload["subagents"] == {"a7b62083": "Explore · find the dispatch path"}


# -- the payload reaches a terminal, a web page and a --json consumer ------------------


def test_a_long_parameter_is_capped_and_the_cap_is_stated(write_transcript):
    """Tool inputs carry whole file contents. Capping is not cosmetic here: the
    truncation has to be VISIBLE and the cap has to travel in the payload, or a reader
    cannot tell a short command from a truncated one."""
    session = write_transcript.write("big", [
        prompt_row(1000, DISPATCH),
        assistant_row(1010, "m1"),
        tool_use_row(1015, "t1", "Write", {"content": "x" * 5000}),
    ])

    _, payload = snap(session, turn_in_flight=True, now=1020.0)

    assert payload["params_cap"] == live.PARAMS_CAP
    assert len(payload["now"]["params"]["content"]) == live.PARAMS_CAP
    assert payload["now"]["params"]["content"].endswith(live.TRUNCATED)


def test_a_parameter_whose_name_says_secret_is_replaced_not_truncated():
    """Truncation is no defence for a credential — the secret is at the FRONT of a
    tokenised remote (`holds.Hold`'s note, kn-1791a5e6). The KEY survives, so the reader
    still sees what the tool was called with."""
    out = live.redact_params({"command": "curl -H auth", "api_key": "sk-live-abcdef",
                              "env": {"GH_TOKEN": "ghp_abcdef"}})

    assert out["api_key"] == live.REDACTED
    assert "sk-live-abcdef" not in json.dumps(out)
    assert "ghp_abcdef" not in out["env"]
    assert out["command"] == "curl -H auth"


def test_a_tokenised_remote_is_scrubbed_before_anything_is_cut(write_transcript):
    """THE LEAK A KEY TEST CANNOT SEE (kn-1791a5e6). `command` is not a secret-sounding
    name and the credential is at the FRONT of the value, so the cap preserves it
    perfectly: this is a `git push` as workers in this fleet actually write one."""
    session = write_transcript.write("push", [
        prompt_row(1000, DISPATCH),
        assistant_row(1010, "m1"),
        tool_use_row(1015, "t1", "Bash", {
            "command": "git push https://x-access-token:ghp_xxx@github.com/acme/p.git "
                       "HEAD:main"}),
    ])

    _, payload = snap(session, turn_in_flight=True, now=1020.0)

    blob = json.dumps(payload)
    assert "ghp_xxx" not in blob and "x-access-token" not in blob
    command = payload["now"]["params"]["command"]
    assert command == ("git push https://<redacted>@github.com/acme/p.git HEAD:main")
    assert payload["now"]["detail"].startswith("git push https://<redacted>@")


def test_an_env_prefixed_token_is_redacted_mid_command(write_transcript):
    """THE SHAPE THE ROUND-1 REVIEW FAILED ON. `GH_TOKEN=ghp_…` is how a worker passes a
    credential to one command, and it is neither a secret-NAMED key nor a URL's userinfo:
    both of round 1's rules miss it. Asserted over the WHOLE payload (kn-637a7236), and
    the command has to stay READABLE — a report that redacts the verb is useless."""
    session = write_transcript.write("envtok", [
        prompt_row(1000, DISPATCH),
        assistant_row(1010, "m1"),
        tool_use_row(1015, "t1", "Bash",
                     {"command": "GH_TOKEN=ghp_abc123 git push origin HEAD"}),
    ])

    _, payload = snap(session, turn_in_flight=True, now=1020.0)

    blob = json.dumps(payload)
    assert "ghp_abc123" not in blob
    assert "git push origin HEAD" in payload["now"]["params"]["command"]
    assert "git push" in payload["now"]["detail"]


def test_an_inline_assignment_is_caught_where_an_anchored_pattern_would_miss(
        write_transcript):
    """A value with no recognisable prefix, mid-command: only the NAME=value rule can
    see it, and only if it is not anchored to the start of a line."""
    session = write_transcript.write("inline", [
        prompt_row(1000, DISPATCH),
        assistant_row(1010, "m1"),
        tool_use_row(1015, "t1", "Bash",
                     {"command": "run && APP_PASSWORD=h7Kq2moPz4 ./deploy.sh"}),
    ])

    _, payload = snap(session, turn_in_flight=True, now=1020.0)

    assert "h7Kq2moPz4" not in json.dumps(payload)
    assert "./deploy.sh" in payload["now"]["params"]["command"]


def test_a_flag_form_credential_is_redacted(write_transcript):
    """THE SAME LEAK ONE SYNTAX ALONG: on a command line a credential arrives as an env
    prefix OR as a flag, and `--password=` escapes a name test anchored to a bare
    identifier exactly as `GH_TOKEN=` escaped an anchored line pattern."""
    session = write_transcript.write("flag", [
        prompt_row(1000, DISPATCH),
        assistant_row(1010, "m1"),
        tool_use_row(1015, "t1", "Bash",
                     {"command": "deploy --password=h7Kq2moPz4 host"}),
    ])

    _, payload = snap(session, turn_in_flight=True, now=1020.0)

    assert "h7Kq2moPz4" not in json.dumps(payload)
    command = payload["now"]["params"]["command"]
    assert command.startswith("deploy ") and command.endswith(" host")


def test_a_bearer_header_inside_a_command_is_redacted(write_transcript):
    """`curl -H 'Authorization: Bearer …'` — the header shape AND the `sk-` value shape,
    either of which must fire before the string reaches params or detail."""
    session = write_transcript.write("bearer", [
        prompt_row(1000, DISPATCH),
        assistant_row(1010, "m1"),
        tool_use_row(1015, "t1", "Bash", {
            "command": "curl -H 'Authorization: Bearer sk-live-xyz' "
                       "https://api.example.com"}),
    ])

    _, payload = snap(session, turn_in_flight=True, now=1020.0)

    assert "sk-live-xyz" not in json.dumps(payload)
    assert "curl" in payload["now"]["params"]["command"]


def test_a_placeholder_value_is_not_redacted_however_secret_its_name():
    """THE NEGATIVE CONTROL, and the reason the name test alone is not enough: every
    repo is full of `token=your-token-here` and `--key=none`, and redacting those makes
    the frame useless for reading what the worker actually ran."""
    out = live.redact_params(
        {"command": "pytest --key=none && TOKEN=your-token-here ./run.sh"})

    assert out["command"] == "pytest --key=none && TOKEN=your-token-here ./run.sh"


def test_a_private_key_block_does_not_survive_under_its_marker():
    """DOTALL or the base64 body sits in the payload underneath the marker
    (kn-097c40ef's first trap)."""
    body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQ"
    out = live.redact_params({"content": "-----BEGIN RSA PRIVATE KEY-----\n"
                                         + body + "\n-----END RSA PRIVATE KEY-----\n"})

    assert body not in out["content"]
    assert "private key block" in out["content"]


def test_a_nested_structure_is_bounded_like_everything_else():
    """A dict of a thousand keys is as expensive to paint as a string of a million
    characters, and a tool input can be either."""
    out = live.redact_params({"edits": [{"old": "y" * 900, "new": "z" * 900}] * 20})

    assert len(out["edits"]) <= live.PARAMS_CAP
    assert out["edits"].endswith(live.TRUNCATED)


# -- the shipped entry point, and the one thing the CLI is allowed to do ----------------


@pytest.fixture()
def registered(jarvis_home, project, tmp_path):
    """One registered project plus a registered catalog — `ops.live_report` resolves the
    work order through the central store and reads the project's `inspect` block."""
    from jarvis.central_store import CentralStore

    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({
        "os": {"cold_prefix_floor": 5_000},
        "projects": [{"name": "proj_a", "path": str(project)}],
    }))
    central = CentralStore()
    try:
        central.upsert_project("proj_a", str(project), "test project")
        central.set_state("catalog_path", str(catalog))
        central.conn.commit()
    finally:
        central.close()
    return project


PAYLOAD_KEYS = {"wo_id", "project", "session_id", "found", "state", "turn", "now",
                "recent", "tokens", "last_write", "stale_seconds", "subagents",
                "holds", "note", "params_cap"}


def test_live_report_publishes_exactly_the_documented_keys(registered,
                                                          write_transcript):
    """The key set is the contract §3 wrote down and both renderers consume unreshaped.
    A key added here is a key the dashboard template may read; one removed is a
    KeyError on a web request."""
    store = ProjectStore(registered)
    try:
        wo = store.create_work_order("watch me", "")
        store.conn.execute("UPDATE work_orders SET session_id=?, status='running' "
                           "WHERE id=?", ("live-sess", wo["id"]))
        store.conn.commit()
        store.create_turn(wo["id"], "dispatch", "go")
    finally:
        store.close()
    write_transcript.write("live-sess", [
        prompt_row(time.time() - 40, DISPATCH),
        assistant_row(time.time() - 30, "m1"),
        tool_use_row(time.time() - 20, "t1", "Bash", {"command": "uv run pytest -q"}),
    ])

    payload = ops.live_report(wo["id"])

    assert set(payload) == PAYLOAD_KEYS
    assert payload["state"] == "working" and payload["now"]["tool"] == "Bash"
    assert payload["wo_id"] == wo["id"] and payload["project"] == "proj_a"


def test_jarvis_watch_once_json_prints_the_payload_unchanged(registered,
                                                             write_transcript, capsys):
    """The renderer derives NOTHING — PR 65's rule. `--json` is the proof available to a
    test: the bytes on stdout are `ops.live_report`'s dict, so a number on the human
    frame can only come from the same place."""
    store = ProjectStore(registered)
    try:
        wo = store.create_work_order("watch me", "")
        store.conn.execute("UPDATE work_orders SET session_id=? WHERE id=?",
                           ("live-sess", wo["id"]))
        store.conn.commit()
    finally:
        store.close()
    write_transcript.write("live-sess", [prompt_row(time.time() - 40, DISPATCH),
                                        assistant_row(time.time() - 30, "m1")])
    expected = ops.live_report(wo["id"])

    assert cli.main(["watch", wo["id"], "--once", "--json"]) == 0

    printed = json.loads(capsys.readouterr().out)
    assert set(printed) == set(expected)
    for key in PAYLOAD_KEYS - {"stale_seconds", "turn", "now"}:
        assert printed[key] == expected[key], key


def test_the_human_frame_shows_the_payload_s_own_values(registered, write_transcript,
                                                        capsys):
    """The non-json frame, which had no test at all. Every assertion is FORMATTED FROM
    THE PAYLOAD rather than written out: the point is that the renderer derives nothing,
    so a literal here would pass against a renderer that computed its own number."""
    store = ProjectStore(registered)
    try:
        wo = store.create_work_order("watch me", "")
        store.conn.execute("UPDATE work_orders SET session_id=?, status='running' "
                           "WHERE id=?", ("live-sess", wo["id"]))
        store.conn.commit()
        store.create_turn(wo["id"], "dispatch", "go")
    finally:
        store.close()
    write_transcript.write("live-sess", [
        prompt_row(time.time() - 40, DISPATCH),
        assistant_row(time.time() - 30, "m1"),
        tool_use_row(time.time() - 20, "t1", "Bash", {"command": "uv run pytest -q"}),
    ])
    payload = ops.live_report(wo["id"])

    cli._print_live(payload)

    out = capsys.readouterr().out
    assert f"{payload['now']['tool'].upper()}" in out
    assert f"{payload['now']['elapsed']:.0f}s" in out
    assert payload["now"]["detail"] in out
    assert f"turn {payload['turn']['seq']}" in out
    assert f"parameters shown to {payload['params_cap']} characters" in out
