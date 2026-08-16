"""The worker prompt must survive the `claude` CLI's own argument parsing.

`--add-dir <directories...>` is a *variadic* option: commander keeps consuming
positionals into it until it hits the next `-`-prefixed token or the `--`
terminator. `spawn_background` appends the prompt positionally, so an
`--add-dir` emitted immediately before it silently swallows the prompt as a
second directory. The session then boots with nothing to do and parks at the
welcome screen forever — "created but never started" (verified live against CLI
2.1.220: the work order prompt never reaches the transcript, and the supervisor
reports `needs: send a prompt to start`).

These tests model commander's parsing rather than asserting a flag order, so
they keep holding if the flag list is reordered or another variadic option is
added later.
"""

from __future__ import annotations

from jarvis import claude_cli

# Arity of every option `spawn_background` can emit, as the `claude` CLI declares
# it (checked against `claude --help` on 2.1.220).
BOOLEAN = {"--bg", "-p"}
# Both are declared with `<xxx...>` and are greedy. `--tools` is the one that bit us
# while verifying this refactor: `claude -p --tools "" "prompt"` swallowed the prompt
# as a tool name and died with "Input must be provided…".
VARIADIC = {"--add-dir", "--tools"}
# everything else below takes exactly one value
SINGLE_VALUE = {
    "--name", "-n", "--resume", "--session-id", "--worktree", "--model", "--effort",
    "--permission-mode", "--append-system-prompt", "--settings", "--output-format",
    # `--autocompact <auto|tokens>` (checked against `claude --help` on 2.1.227)
    "--autocompact",
}


def commander_positionals(argv: list[str]) -> list[str]:
    """Recover the positional args the way commander.js would.

    Greedy for variadic options, and `--` ends option parsing entirely.
    """
    positionals: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            positionals.extend(argv[i + 1:])
            break
        if tok in BOOLEAN:
            i += 1
        elif tok in SINGLE_VALUE:
            i += 2
        elif tok in VARIADIC:
            i += 1
            while i < len(argv) and not argv[i].startswith("-"):
                i += 1  # swallowed as a directory
        else:
            assert not tok.startswith("--"), (
                f"unmodelled option {tok!r}: add it to this test's arity table"
            )
            positionals.append(tok)
            i += 1
    return positionals


def _argv(fake_claude) -> list[str]:
    assert fake_claude.calls, "the fake claude binary was never invoked"
    return fake_claude.calls[-1]["argv"]


def test_prompt_survives_add_dir(fake_claude, tmp_path) -> None:
    """The regression: --add-dir must not eat the prompt."""
    skills = tmp_path / "agent-skills"
    skills.mkdir()
    prompt = "You are the worker agent for Jarvis work order `wo-abc123`."

    claude_cli.spawn_background(
        prompt=prompt, cwd=tmp_path, name="[WO wo-abc123] title",
        add_dirs=[skills],
    )

    argv = _argv(fake_claude)
    assert commander_positionals(argv) == [prompt], (
        "the prompt was consumed by a variadic option instead of arriving as the "
        f"positional prompt; argv={argv}"
    )


def test_prompt_survives_the_full_dispatch_flag_set(fake_claude, tmp_path) -> None:
    """Every flag dispatch.py can emit, together — the real worker shape."""
    skills = tmp_path / "agent-skills"
    skills.mkdir()
    settings = tmp_path / "wo-abc123.json"
    settings.write_text("{}")
    prompt = "Do the thing.\nAnd report back."

    claude_cli.spawn_background(
        prompt=prompt, cwd=tmp_path, name="[WO wo-abc123] title",
        model="claude-opus-5", effort="high", permission_mode="auto",
        append_system_prompt="extra", worktree="wo-abc123",
        settings_file=settings, add_dirs=[skills],
    )

    argv = _argv(fake_claude)
    assert commander_positionals(argv) == [prompt], f"argv={argv}"


def test_add_dirs_still_reach_the_cli(fake_claude, tmp_path) -> None:
    """Guard the fix against the lazy cure of simply dropping --add-dir."""
    a, b = tmp_path / "skills-a", tmp_path / "skills-b"
    a.mkdir()
    b.mkdir()

    claude_cli.spawn_background(
        prompt="hi", cwd=tmp_path, name="n", add_dirs=[a, b],
    )

    argv = _argv(fake_claude)
    assert argv.count("--add-dir") == 2
    assert str(a) in argv and str(b) in argv


def test_prompt_is_never_left_adjacent_to_a_variadic_option(fake_claude, tmp_path) -> None:
    """Structural guard: the prompt must be fenced off by `--`.

    Reordering flags so --add-dir merely stops being *last* would fix today's
    symptom and break again the next time a flag is appended. Requiring the
    terminator makes the prompt position-independent.
    """
    skills = tmp_path / "agent-skills"
    skills.mkdir()

    claude_cli.spawn_background(
        prompt="hi", cwd=tmp_path, name="n", add_dirs=[skills],
    )

    argv = _argv(fake_claude)
    assert "--" in argv, f"no `--` terminator before the prompt; argv={argv}"
    assert argv[argv.index("--") + 1:] == ["hi"], (
        f"exactly the prompt must follow the terminator; argv={argv}"
    )


def test_prompt_beginning_with_a_dash_is_not_read_as_a_flag(fake_claude, tmp_path) -> None:
    """`--` also protects prompts whose first character is a dash."""
    claude_cli.spawn_background(
        prompt="--version is what the user asked about", cwd=tmp_path, name="n",
    )

    argv = _argv(fake_claude)
    assert commander_positionals(argv) == ["--version is what the user asked about"], (
        f"argv={argv}"
    )


# -- worker turns (the transport workers actually run on) ------------------------------


def turn_argv(**kwargs) -> list[str]:
    defaults = dict(prompt="Do the thing.", session_id="11111111-2222-4333-8444-555555555555",
                    resume=False)
    return claude_cli.turn_args(**{**defaults, **kwargs})


def test_turn_prompt_survives_the_full_flag_set(tmp_path) -> None:
    """The same variadic trap, on the path every worker turn now takes."""
    skills = tmp_path / "agent-skills"
    skills.mkdir()
    settings = tmp_path / "wo-abc123.json"
    settings.write_text("{}")
    prompt = "Do the thing.\nAnd report back."

    argv = turn_argv(
        prompt=prompt, name="[WO wo-abc123] title", worktree="wo-abc123",
        model="claude-opus-5", effort="high", permission_mode="auto",
        append_system_prompt="extra", settings_file=settings, add_dirs=[skills],
    )

    assert commander_positionals(argv) == [prompt], f"argv={argv}"
    assert argv[-2] == "--", f"no `--` terminator before the prompt; argv={argv}"


def test_opening_turn_assigns_the_id_and_later_turns_resume_it() -> None:
    """`--session-id` opens the conversation, `--resume` continues it — same id.

    Verified against CLI 2.1.220: a headless resume reuses the id it is given rather
    than forking (that is what `--fork-session` is for), which is the property the whole
    work-order↔session binding now rests on.
    """
    sid = "11111111-2222-4333-8444-555555555555"

    first = turn_argv(resume=False)
    later = turn_argv(resume=True)

    assert first[first.index("--session-id") + 1] == sid
    assert "--resume" not in first
    assert later[later.index("--resume") + 1] == sid
    assert "--session-id" not in later
    for argv in (first, later):
        assert argv[:3] == ["-p", "--output-format", "json"]


def test_turn_never_enters_the_background_roster() -> None:
    """A worker turn must never be spawned with --bg: that is the transport whose
    leaked sessions this replaced."""
    assert "--bg" not in turn_argv(resume=False)
    assert "--bg" not in turn_argv(resume=True)


def test_worktree_is_only_created_on_the_opening_turn(tmp_path) -> None:
    """Turn one creates the worktree and starts in it; later turns run with it as cwd,
    so passing the flag again would be asking for a worktree that already exists."""
    assert "--worktree" in turn_argv(resume=False, worktree="wo-abc123")
    assert "--worktree" not in turn_argv(resume=True)


# -- the context bound ------------------------------------------------------------------


def test_autocompact_rides_on_every_turn_including_resumes() -> None:
    """The context bound must be re-passed on turn 2+, not just at dispatch.

    A resumed session re-derives its flags from argv (kn-6352bc0f). Passing
    `--autocompact` only on the opening turn would let the conversation past the bound
    from turn two onwards — which is precisely the turn range that grows, and precisely
    the range this flag exists to bound.
    """
    for resume in (False, True):
        argv = turn_argv(resume=resume, autocompact_window=150_000)
        assert argv[argv.index("--autocompact") + 1] == "150000", (
            f"resume={resume}: argv={argv}")


def test_no_autocompact_flag_when_the_project_opted_out() -> None:
    """None means "say nothing", so the CLI's own default (the model's window) stands.

    Emitting `--autocompact 0` or `--autocompact auto` instead would be a different
    thing: the CLI rejects 0, and `auto` is a mode, not "unset".
    """
    assert "--autocompact" not in turn_argv(resume=True, autocompact_window=None)


def test_autocompact_reaches_every_launch_path(fake_claude, tmp_path) -> None:
    """Guard the shared helper: both spawners must emit it, not just the turn path.

    `_briefing_args` exists so a new way to start a worker cannot forget a flag. A fix
    applied to `turn_args` alone would pass the test above and still silently drop the
    bound on any other path.
    """
    claude_cli.spawn_background(prompt="hi", cwd=tmp_path, name="n",
                                autocompact_window=150_000)
    assert "--autocompact" in _argv(fake_claude)
    assert "--autocompact" in turn_argv(resume=True, autocompact_window=150_000)


def test_the_prompt_survives_the_bound(tmp_path) -> None:
    """The new flag is single-valued, so it must not eat the prompt."""
    prompt = "Do the thing."
    argv = turn_argv(prompt=prompt, autocompact_window=150_000,
                     add_dirs=[tmp_path])
    assert commander_positionals(argv) == [prompt], f"argv={argv}"
