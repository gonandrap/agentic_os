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
BOOLEAN = {"--bg"}
VARIADIC = {"--add-dir"}  # declared `--add-dir <directories...>` — greedy
# everything else below takes exactly one value
SINGLE_VALUE = {
    "--name", "--resume", "--worktree", "--model", "--effort",
    "--permission-mode", "--append-system-prompt", "--settings",
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
