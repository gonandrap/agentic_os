# Onboarding help in a terminal side panel

Feature order `fo-ce02d84a`, filed from improvement order `io-74eab254` finding
`help-is-not-a-panel` (learning `kn-acfd1f34`).

**The user's ask, verbatim (io-74eab254 item 1):** "when I asked for help, it created a
background session and told me to attach to it from the console. That is not what I
requested as feature, I want a PTY console in a side panel, so the user can see both the
UI of the onboarding as well as the terminal".

**Outcome.** On any onboarding section page, "Ask for help" (and every knob's `?` button)
opens a resizable panel on the right of the form. The panel is a real terminal (xterm.js)
talking over a websocket to a PTY in the dashboard process, which runs the interactive
`claude --agent onboard_helper` with the same prompt, cwd and add-dirs the background path
uses today. The user types into it and reads answers while the form stays visible. One
helper per draft. Moving to another section keeps it, and asking for help again feeds the
new context into the same conversation. Closing the panel, leaving it idle, or stopping
the dashboard kills the process. No process outlives its owner. When the PTY cannot start,
today's background session (`POST /onboard/{name}/help`, spec
`2026-11-09-onboarding-from-the-dashboard.md` §4) is the fallback, unchanged.

---

## 0. What exists, and the contract every section shares

**Today** (spec `2026-11-09-onboarding-from-the-dashboard.md` §4):
`src/jarvis/ui/onboard_help.py` registers `POST /onboard/{name}/help` (form fields
`section`, `focus`). It calls `ops.onboard_help(name, section, focus)` in
`src/jarvis/ops.py`, which builds the prompt with `_onboard_help_prompt(view, sec, focus,
url)`, rebuilds the persona root with `bootstrap._rebuild(bootstrap.ASSETS /
"onboard-helper", paths.onboarding_dir() / "helper", "agents")` and spawns
`claude_cli.spawn_background(..., agent=ONBOARD_HELPER, add_dirs=[root, project_path],
cwd=paths.jarvis_home())`. `templates/onboard_help.html` shows the attach command or the
failure. The `help_form(focus, label, title)` macro in `templates/onboard_section.html`
posts that form.

**Facts the design rests on (verified on this branch, 2026-11-12):**
- The venv has no websocket library. Neither `websockets` nor `wsproto` imports, so
  uvicorn cannot upgrade a websocket today. The `ui` extra in `pyproject.toml` is
  fastapi, uvicorn, jinja2 and python-multipart. Production builds with `uv sync --frozen
  --extra ui` (`scripts/shipit.sh`), so a dependency missing from `uv.lock` never reaches
  prod.
- The dashboard has no websocket, no CSRF token and no Origin check anywhere.
- `src/jarvis/ui/vendor/README.md`: "The dashboard is local-first: it loads no script from
  a CDN at runtime". Libraries are vendored with a pinned version and sha256, and served by
  `GET /vendor/{name}` in `app.py`. That route only knows `*.js` (`VENDOR_VERSION` globs
  `*.js`) and serves `text/javascript`.
- `create_app` owns an `asynccontextmanager` `lifespan` (anyio task group, watchdog). The
  UI has wedged before on a blocked event loop (`kn-ad7a0b54`), so nothing here may do a
  blocking read on the loop.
- `deploy/jarvis-ui.service.template` runs `jarvis ui --host 127.0.0.1` with
  `Restart=always` and the default `KillMode=control-group`. systemd therefore kills every
  descendant on stop or restart. A macOS launchd plist and a plain `jarvis ui` from a
  terminal have no such net.
- `jarvis learn review` serves a SECOND `create_app` in another process
  (`cli._serve_dashboard`). Two dashboards can run at once.
- The hooks act on `JARVIS_TURN_TRANSPORT` only when it is `headless` or `JARVIS_WO_ID` is
  set (`kn-d3fb9617`). A session with neither is left alone.
- The persona `src/jarvis/assets/onboard-helper/onboard_helper.md` declares `tools: Bash,
  Read`. In an interactive session the user can approve a tool call in the terminal, so
  read-only must hold by flags, not by prose.
- `tests_browser/` is a Playwright suite (uvicorn in a thread, `fake_claude` fixture) that
  CI runs (`pytest tests_browser -q`). `FAKE_CLAUDE` in `src/jarvis/testing.py` has no
  interactive mode.
- A worker cannot drive an interactive TTY `claude` (`kn-05a361d4`).

### 0.1 Non-goals

- No app-wide CSRF scheme, no auth, and no change to any other page's forms.
- No change to the systemd or launchd unit files.
- No terminal anywhere but the onboarding section pages.
- No change to what the helper is allowed to know or say (persona prose, prompt builder).
- No new `jarvis` CLI subcommand or catalog knob. The idle timeout is a module constant.

### 0.2 Names and interfaces — frozen

| Thing | Name | Owner |
|---|---|---|
| launch description | `ops.onboard_help_launch(name, section, focus="", *, catalog_path=None) -> dict` | §1 |
| PTY registry module | `src/jarvis/ui/help_pty.py`: `HelpSessions`, `HelpSession` | §1 |
| fake interactive claude | `FAKE_CLAUDE` interactive mode in `src/jarvis/testing.py` | §1 |
| routes module | `src/jarvis/ui/onboard_help_pty.py`, `register(app, render)` | §2 |
| panel markup | `templates/onboard_help_panel.html` (included once per section page) | §3 |
| panel script | `src/jarvis/ui/static/help_panel.js` | §3 |
| vendored terminal | `src/jarvis/ui/vendor/xterm.js`, `xterm.css`, `xterm-addon-fit.js` | §3 |

`onboard_help_launch` returns:
`{"argv": list[str], "cwd": Path, "env": dict[str, str], "prompt": str, "session_name":
str}`.

`HelpSessions(idle_seconds: float = IDLE_SECONDS, owner_dir: Path | None = None)`, built
inside the running event loop:
- `async start_or_feed(draft: str, launch: dict) -> tuple[HelpSession, bool]`. It returns
  the live session for `draft` (`created=False`, after feeding `launch["prompt"]`), or
  spawns one (`created=True`). It raises `HelpPtyError` when the spawn fails.
- `get(draft) -> HelpSession | None`. It never returns a dead session.
- `async close(draft) -> bool`, `async close_all() -> None`.
- `reap_stale() -> list[int]`. It kills pids recorded by a dashboard process that no
  longer exists, and returns them.
- `live_pids() -> list[int]` for tests and doctor-style checks.

`HelpSession`: `token: str`, `draft: str`, `pid: int`, `alive: bool`, `write(data:
bytes)`, `resize(rows: int, cols: int)`, `attach() -> tuple[bytes, asyncio.Queue[bytes |
None]]` (scrollback, then live output; `None` = process ended), `detach(queue)`.

Websocket protocol: `WS /onboard/{name}/help/pty/ws?token=<token>`.
- Server to client: binary frames of PTY output. The first frame is the scrollback.
- Client to server: binary frames are keystrokes. A text frame `{"resize": [rows, cols]}`
  resizes the terminal.
- Close codes: `4403` bad Origin or token, `4404` no live session for the draft, `4409`
  replaced by a newer attach, `1000` the helper process ended.

HTTP: `POST /onboard/{name}/help/pty` (form `section`, `focus`) returns `200 {"token",
"ws_url", "created", "session_name"}`, `403` bad Origin, `404` unknown draft or section,
or `502 {"error"}` when the PTY could not start. `POST /onboard/{name}/help/pty/close`
(form `token`) returns `200 {"closed": bool}`.

### 0.3 Traps every section inherits

1. **Never block the loop.** Read PTY output with `loop.add_reader` on the master fd. Put
   it into bounded per-subscriber queues. A slow socket drops its oldest chunk, never
   stalls the reader. A route that touches the registry is `async def`, because sync
   routes run in the threadpool, off the loop.
2. **No orphans, in three layers.** (a) The child runs in its own session (`os.setsid`)
   with `PR_SET_PDEATHSIG = SIGTERM` on Linux (through `ctypes`, skipped elsewhere), and
   it is killed with `os.killpg`: SIGTERM, then SIGKILL after 2 s, then `waitpid`.
   (b) `close_all()` runs in the lifespan teardown. (c) Each child gets a record
   `<owner_dir>/<dashboard pid>-<child pid>.json`, where `owner_dir` defaults to
   `paths.jarvis_home() / "ui" / "help-pty"`. `reap_stale()` at startup kills only the
   children whose dashboard pid is dead. Two dashboards can run at once, so never reap
   another live dashboard's helpers.
3. **Read-only by flags.** The interactive argv carries `--disallowedTools Edit Write
   NotebookEdit` and allows the persona's read verbs without a prompt (§1). The user can
   still approve some other Bash command in the terminal. That is the user acting, not the
   helper, and it is out of scope.
4. **Environment.** The child env is `os.environ` minus `JARVIS_WO_ID` and
   `JARVIS_TURN_TRANSPORT`, plus `TERM=xterm-256color` and `COLORTERM=truecolor`. A
   dashboard started from a worker shell must not make the helper look like a worker to
   the hooks.
5. **Worker lockout in tests.** A test that writes a draft copies the autouse
   `not_a_worker` fixture (`tests/test_config_console.py`, `kn-650b6f24`). Run only your
   own test files (`kn-356c724b`). No test execs the real `claude`.

### 0.4 What only the user can verify

A worker cannot drive an interactive `claude` (`kn-05a361d4`), so the last PR of this
feature (§3) names these steps in its "How to validate in production" section. The user
runs them after the release, on the production dashboard (the systemd `jarvis-ui`
unit):
1. Open an onboarding draft. Click "Ask for help". The panel shows the `@onboard_helper`
   banner, and typing a question gets an answer. This also proves `claude_bin()` resolves
   under the unit's `PATH`.
2. Go to another section, then click a knob's `?`. The same conversation continues and
   receives the new context.
3. Close the panel. `pgrep -f 'agent onboard_helper'` prints nothing.
4. Open the panel and run `systemctl --user restart jarvis-ui`. No helper process
   survives.

---

## 1. The PTY session registry and the interactive launch

**Owns:** `ops.onboard_help_launch` (new) and its extraction from `ops.onboard_help`;
`src/jarvis/ui/help_pty.py` (new); the interactive mode of `FAKE_CLAUDE` in
`src/jarvis/testing.py`; `tests/test_help_pty.py` (new). Depends on nothing.

**`ops.onboard_help_launch`.** Move the shared head of `ops.onboard_help` into it: the
section lookup and its `OpsError`, the URL, `_onboard_help_prompt`, the `bootstrap._rebuild`
of the helper root, add-dirs, and `session_name = f"[onboard] {name} · {section}"`. Then
`onboard_help` calls it, so the PTY path and the background fallback cannot drift.
`onboard_help`'s return value and behaviour stay byte-identical, and
`tests/test_onboard_help.py` must pass unmodified. The interactive argv is:

```
[claude_bin(), "--name", session_name, "--agent", ONBOARD_HELPER,
 "--add-dir", <helper root>, "--add-dir", <project path>,
 "--disallowedTools", "Edit", "Write", "NotebookEdit",
 "--allowedTools", "Bash(jarvis onboard explain:*)",
 "Bash(jarvis onboard questions:*)", "Bash(jarvis config show:*)",
 "Bash(jarvis config get:*)", "Read",
 "--", prompt]
```

The allow list is narrow on purpose. `Bash(jarvis onboard:*)` would let `jarvis onboard
apply` run with no prompt, and that command writes. `apply --dry-run` is deliberately
absent, because its flag order varies and a prefix pattern cannot pin it. It runs after a
prompt the user answers.

There is no `--bg` and no `-p`. `cwd` is `paths.jarvis_home()`, not the project, because
the project's workspace trust is written only by the bootstrap. `env` follows §0.3 item 4.

**`help_pty.py`** implements the §0.2 interface with the stdlib only (`pty`/`os.openpty`,
`fcntl`, `termios`, `struct`, `signal`, `asyncio`). No new dependency, and no import of
FastAPI or `ui.app`.
- Spawn: `os.openpty()`, then `subprocess.Popen(argv, cwd, env, stdin/stdout/stderr=slave,
  preexec_fn=_child_setup)`. `_child_setup` runs `os.setsid()`, sets the controlling tty
  (`TIOCSCTTY`) and sets PDEATHSIG on Linux. Close the slave in the parent. Set the master
  non-blocking. Initial size is 24x80 until the first resize. A spawn `OSError` (missing
  binary included) raises `HelpPtyError` with the OS's message. Spawn on the loop's thread,
  never in a worker thread: PDEATHSIG fires when the spawning THREAD exits, so a child
  spawned from a short-lived threadpool thread dies at once.
- Reader: `loop.add_reader(master, ...)` reads up to 64 KiB and appends it to a scrollback
  ring of 256 KiB. It puts the chunk on every attached queue (`maxsize=256`; when one is
  full, drop that queue's oldest item). On EOF or `EIO` the process has ended: remove the
  reader, reap, mark it dead, push `None` to each queue, delete the pid record and drop it
  from the registry.
- Feed: bracketed paste (`\x1b[200~` + text + `\x1b[201~`) then `\r`, so a multi-line
  context arrives as one message. The fed text is `"[context] The user is now on section
  <section>" + (", asking about <focus>" if focus) + ".\n\n" + launch["prompt"]`.
- Single attach: `attach()` while another queue is attached is allowed. The caller (§2)
  closes the old socket with 4409. The registry only needs to track the newest queue as
  "the" attachment for idle purposes.
- Idle: a session is idle when it has had no attached queue AND no input for
  `idle_seconds`. Default `IDLE_SECONDS = 15 * 60`, a module constant. The constructor
  starts one asyncio task per registry (it is built inside the running loop). That task
  checks every `min(30, idle_seconds / 2)` seconds and closes idle sessions. `close_all()`
  cancels it.
- Resize: `fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))`,
  clamped to 1..500.
- `token` is `secrets.token_urlsafe(24)`, created once per session.

**Fake interactive claude.** When `FAKE_CLAUDE` is invoked without `--bg`, `-p`/`--print`
or any subcommand, it is "interactive". It records the call like any other (argv, cwd,
selected env), prints the banner `fake-claude interactive @<agent>`, then reads stdin
line by line. For each line it prints `echo: <line>`, and it exits 0 on `/exit` or EOF.
With bracketed paste it prints `pasted: <first line of the paste>`. On SIGWINCH it prints
`size: <rows> <cols>`, read from its tty. Keep it as small as possible. §2's and §3's tests
depend on exactly these strings.

**Tests** (`tests/test_help_pty.py`, fake claude only). The repo has no pytest-asyncio, so
use `@pytest.mark.anyio` (anyio's bundled plugin) or `asyncio.run`. Every wait polls
against a deadline and never blocks unbounded.
1. `onboard_help_launch` argv carries `--agent onboard_helper`, both `--add-dir`s, the
   `--disallowedTools` set, the exact `--allowedTools` set, no `--bg`, and the prompt last.
   No allowed pattern is a prefix of `jarvis onboard apply` or `jarvis config set`.
   Control: `onboard_help`'s argv still carries `--bg`.
2. Start a session. Write `hello\r`. The attached queue yields bytes containing `echo:
   hello` within 5 s.
3. `start_or_feed` twice for one draft gives the same pid and `created` True then False.
   The second call's queue sees `pasted: [context] The user is now on section`.
4. `close(draft)`: the pid is gone (`os.kill(pid, 0)` raises), its record file is deleted
   and `live_pids()` is empty. Control: before the close, the pid was alive.
5. Idle: with `idle_seconds=0.2` and no attachment, the process dies within 2 s.
   Control: an attached session with the same setting survives the same wait.
6. `reap_stale`: a record naming a dead dashboard pid plus a live `sleep 60` child gets
   that child killed. A record naming the current pid is left alone.
7. Env: the child's recorded env lacks `JARVIS_WO_ID` and `JARVIS_TURN_TRANSPORT` while the
   parent has both set.
8. Parent killed outright (Linux only, `skipif` elsewhere): a subprocess runs a
   `HelpSessions`, starts a helper and prints its pid. SIGKILL that subprocess. The helper
   is dead within 5 s (PDEATHSIG). Control: before the SIGKILL, the helper was alive.
9. `resize(30, 100)` makes the attached queue see `size: 30 100`.

## 2. Transport: the websocket, the routes and the lifespan

**Owns:** the `websockets` dependency (`pyproject.toml` `ui` and `dev` extras plus
`uv.lock`, regenerated with `uv lock`, never hand-merged); `src/jarvis/ui/onboard_help_pty.py`
(new) and its `register` call in `create_app`; the lifespan additions in
`src/jarvis/ui/app.py`; `tests/test_onboard_help_pty.py` (new). Depends on §1.

**Lifespan.** Inside the task group, build `app.state.help_sessions = HelpSessions()`,
call `reap_stale()` (log what it killed), and start the idle task. In the `finally`, run
`await help_sessions.close_all()` before the task group is cancelled. A failure in either
must not stop the dashboard. Log it and continue, like `invariants.report_at_startup`.

**Origin check** (`_origin_ok(request_or_ws) -> bool`): an `Origin` header must be present.
Its scheme://host:port must equal either the request's own `Host` on a loopback host
(`127.0.0.1`, `localhost`, `[::1]`) or the origin of `notify.ui_base_url(catalog)` (the
dashboard may be reached by a tailnet or phone hostname). Anything else gets 403 on HTTP
and close 4403 on the websocket. This is the CSRF defence for the two POSTs. The websocket
also requires the session's token. Both checks live in this module only (§0.1).

**Routes**, all `async def`, all registered before any catch-all `/onboard/{name}/{section}`
route:
- `POST /onboard/{name}/help/pty`: Origin check, then `ops.onboard_help_launch` (run it with
  `anyio.to_thread.run_sync`, because it reads files), then
  `help_sessions.start_or_feed(name, launch)`. `OpsError` gives 404 `{"error"}`.
  `HelpPtyError` gives 502 `{"error"}`, and the page then falls back (§3). Success gives
  200 with the §0.2 body. `ws_url` is the path only, and the browser builds `ws(s)://` from
  `location`.
- `WS /onboard/{name}/help/pty/ws`: Origin check, then token equals the live session's
  token (else 4403), then a live session exists (else 4404). Accept, `attach()`, send the
  scrollback as one binary frame, then run two tasks. Pump the queue to the socket, and on
  `None` close with 1000. Read the socket: binary goes to `write`, a JSON resize goes to
  `resize`, and malformed text is ignored. On disconnect, `detach`. Never close the session
  on disconnect, because navigation is a disconnect. A newer attach for the same draft
  closes the older socket with 4409.
- `POST /onboard/{name}/help/pty/close` (form `token`): Origin check, token match, then
  `close(name)`, which returns `{"closed": true}`. A stale token returns `{"closed":
  false}` with 200, since the panel may be closing a session that already ended.

**Tests** (`tests/test_onboard_help_pty.py`, Starlette `TestClient`, fake claude, the
`not_a_worker` fixture). Two harness traps:
- Use `TestClient(app, base_url="http://127.0.0.1")`. The default `Host: testserver` is not
  loopback, so the loopback control would get 403.
- Always use `with TestClient(...) as c:`, which runs the lifespan and keeps one portal
  thread. Without it, each request's short-lived thread exit fires the child's PDEATHSIG.
- `receive_bytes()` has no timeout. Read frames in a loop until the expected text, with a
  deadline enforced off-thread, so a missing echo fails the test instead of hanging CI.

1. POST without `Origin` gets 403. POST with `Origin: http://evil.example` gets 403.
   Control: POST with the loopback origin gets 200 and a token. The origin of
   `notify.ui_base_url` (set to a non-loopback host in the test catalog) also gets 200.
2. WS with a wrong token is closed 4403, and a right token with a bad Origin is also 4403.
   WS for a draft with no live session gets 4404. Control: the right token and Origin
   receive the scrollback frame containing `fake-claude interactive @onboard_helper`.
3. Type and receive over the socket: send `b"hi\r"`, receive a frame containing `echo: hi`.
4. A second POST for the same draft returns the same token with `created: false`, and the
   attached socket receives `pasted: [context]`.
5. Disconnecting the socket leaves the pid alive. POST close kills it (`os.kill(pid, 0)`
   raises).
6. Lifespan: leaving the `TestClient` context (shutdown) leaves no pid from
   `live_pids()` alive.
7. A launch whose `claude` binary does not exist (point `JARVIS_CLAUDE_BIN` at a missing
   path) gets 502 with the OS error text.
8. A second WS attach for the same draft closes the first with 4409, and the second keeps
   receiving.
9. Close POST with a bad Origin gets 403, and close with a stale token gets 200
   `{"closed": false}`. Both leave the live pid alive (control: the right token kills it).
10. Lifespan startup reaps a stale record: plant a record naming a dead dashboard pid and a
    live `sleep 60` child before entering the client. The child is dead after startup.
11. The text frame `{"resize": [30, 100]}` makes the socket receive
    `size: 30 100`.

## 3. The panel: vendored xterm.js, the side panel and the browser test

**Owns:** the vendored files `src/jarvis/ui/vendor/xterm.js`, `xterm.css`,
`xterm-addon-fit.js` (pinned versions from cdn.jsdelivr.net, sha256 and license added to
`vendor/README.md` and `LICENSES.txt`); widening the `/vendor/` route to serve `.css` as
`text/css` (`VENDOR_VERSION`/`VENDORED` included, and update
`tests/test_ui_knowledge_review.py`'s `VENDORED` assertion if it breaks);
`templates/onboard_help_panel.html` (new); `src/jarvis/ui/static/help_panel.js` (new);
one include line in `templates/onboard_section.html`; an optional `pty_error` form field
on today's `POST /onboard/{name}/help` shown on `onboard_help.html`;
`tests_browser/test_onboard_help_panel.py` (new). Depends on §2.

**Why vendored, not a CDN.** The dashboard's stated rule is "loads no script from a CDN at
runtime". Pinning the version and recording the sha256 meets the "pinned" intent of the
ask without breaking that rule.

**Layout.** When the panel is open, the section page becomes two columns: the form on the
left, and the panel on the right at 40% width by default. A vertical drag handle between
them resizes the panel (min 280 px, max 70% of the viewport). The width is kept in
`localStorage`, wrapped in try/catch. Every drag end and every window resize runs the fit
addon and sends `{"resize": [rows, cols]}`. The panel header shows the session name, a
status line ("connecting", "live", "ended", or the error) and a Close button.

**Behaviour** (`help_panel.js`, no framework, no build step):
- Progressive: `help_form` stays a normal form posting to `/onboard/{name}/help`, which is
  today's background path, so the page still works without JS. The script intercepts
  submit, `fetch`es `POST …/help/pty` with the same fields, and opens the panel.
- Reattach across navigation: store `{token, ws_url}` in `sessionStorage` under the draft
  name. On every section page load, if a token is stored, open the panel and connect. Close
  4404 or 4403 means the session is gone: clear the entry and hide the panel. Close 1000
  shows "help session ended" with a "start again" button. Close 4409 shows "opened in
  another tab".
- A second help click while the panel is open posts again. The server feeds the context,
  so the panel stays and only refocuses the terminal.
- Close button: `POST …/help/pty/close` with the token, clear the entry, hide the panel.
  Also `navigator.sendBeacon` the close on `pagehide` ONLY when the tab is closing, never on
  in-site navigation. If that cannot be told apart reliably, rely on the idle timeout and
  say so in the PR.
- Fallback: on a 502, submit the original form with a hidden `pty_error` field holding the
  error. The background session starts as today, and `onboard_help.html` shows
  "The terminal panel could not start: <error>" above its existing content. Never fall
  back silently (`kn-53a20d3a`).

**Tests** (`tests_browser/test_onboard_help_panel.py`, the existing `server` fixture plus
an onboarding draft, fake claude in interactive mode from §1). The browser fixture cannot
reach `app.state`, so find the helper pid from the record files under
`<JARVIS_HOME>/ui/help-pty/` (`<dashboard pid>-<child pid>.json`). Every wait polls the
xterm buffer with a deadline and never sleeps blindly.
1. Type and receive: open a section, click "Ask for help", wait for `fake-claude
   interactive @onboard_helper` in the terminal, type `hello` + Enter, see `echo: hello`.
   Read the terminal text through xterm's buffer API (`term.buffer.active`) in
   `page.evaluate`, not `inner_text`. Expose the `Terminal` on `window` for this.
2. Navigation: go to another section. The panel reopens with the earlier scrollback, and
   there is still exactly one record file with the same child pid. Click a knob's `?` and
   the terminal shows `pasted: [context]`.
3. No PTY after close: click Close. Within 5 s, the record file is gone and the child pid
   is dead. Control: before the close, it was alive.
4. Resize: dragging the handle makes the terminal show a `size: R C` line that differs from
   the one before the drag.
5. Fallback (browser): with `JARVIS_CLAUDE_BIN` pointed at a missing path, "Ask for help"
   lands on the background-session page showing "The terminal panel could not start:"
   followed by the error.
6. In `tests/` (TestClient, no browser): `GET /vendor/xterm.css` returns 200 with
   `content-type` starting `text/css`, and `/vendor/xterm.js` still returns
   `text/javascript`.

The PR's "How to validate in production" section lists §0.4's four steps for the user.

---

## Agent profile

You are a Jarvis OS engineer implementing ONE section of "onboarding help in a terminal
side panel". You work in your own git worktree on the OS's own repository, open one pull
request, and touch only the files your section names. Your section is your brief. §0 of
this spec is the contract every section shares. Read it before writing anything, and
never change a §0.2 name or signature. If one is wrong, say so in your finish summary
instead.

**What you must know about this codebase.** The CLI is the OS: state is read and written
through `ops.py`, which both `cli.py` and the FastAPI dashboard (`src/jarvis/ui/`) call.
Imports run strictly downward: leaves (`paths`, `claude_cli`, `catalog`), then `ops`, then
`cli`/`ui`. `ops` never imports `ui`. A `ui/*.py` route module exposes `register(app,
render)` and imports `ui.app` helpers lazily inside its handlers, because `app.py` imports
the module. `create_app` in `src/jarvis/ui/app.py` owns a `lifespan` with an anyio task
group. The dashboard has wedged before because its event loop was blocked (learning
`kn-ad7a0b54`): no blocking call on the loop, ever. The dashboard is local-first and
vendors its browser libraries under `src/jarvis/ui/vendor/` with pinned versions and
sha256. It never loads from a CDN at runtime. Production installs with `uv sync --frozen
--extra ui`, so a dependency must be in `uv.lock` and the `ui` extra.

**Conventions.** House style is `caveman` and `ponytail` at level `full` for everything
you write: code comments state only the why, in one line. Make the smallest change that
fully solves your section, including the callers and tests it breaks. Run only your own
test files (`uv run pytest tests/<file> -q`, or `uv run pytest tests_browser/<file> -q`).
Never run the full suite, which is CI's job. Tests use `jarvis/testing.py`'s `fake_claude`
and never exec the real `claude`. Every "must not / is gone" assertion has a control in the
same test showing the positive case. A test that writes an onboarding draft carries the
autouse `not_a_worker` fixture from `tests/test_config_console.py`. Your PR body follows
the `open-a-pull-request` skill and shows the test evidence: what ran and its output.

**Traps that cost work orders before, or would here.**
- Sync FastAPI routes run in a threadpool, off the loop. Anything that touches the PTY
  registry is `async def`, and blocking file work inside one goes through
  `anyio.to_thread.run_sync`.
- systemd's `KillMode=control-group` hides orphan bugs in production. Test the no-orphan
  path with a plain process, as a launchd or terminal-started dashboard would run.
- Two dashboards can run at once (`jarvis learn review` starts its own). Never kill a
  helper whose recorded dashboard pid is still alive.
- The helper runs in an environment with neither `JARVIS_WO_ID` nor
  `JARVIS_TURN_TRANSPORT`, or the OS hooks treat it as a worker.
- xterm draws to a canvas or DOM rows that `inner_text` misreads. Read the buffer through
  `term.buffer.active` in `page.evaluate`. `base.html` inlines the CSS, so asserting a class
  name passes vacuously: assert the element or the behaviour.
- Starlette matches routes in registration order. Register the `/help/pty` routes before
  any `/onboard/{name}/{section}` catch-all.
- `uv.lock` conflicts with other in-flight orders: regenerate it with `uv lock`, never
  hand-merge it.
- When the OS does nothing (spawn failed, session gone), the page says so with the real
  error. Silence is worse than a bad answer (`kn-53a20d3a`).

**Never.** Do not implement a sibling's section, not even two lines of it. Name the gap in
your finish summary. Do not edit the systemd or launchd unit templates, the daemon,
`hooks.py`, the persona prose or `_onboard_help_prompt`. Do not add app-wide CSRF, auth, a
CLI subcommand or a catalog knob. Do not let the helper write: no `Edit`/`Write` tools, no
`jarvis config set`, no `jarvis onboard apply` without `--dry-run`. Do not load anything
from a CDN at runtime. Do not drive a real interactive `claude` from your worker session:
the isolation guard refuses it (`kn-05a361d4`), and that check belongs to the user (§0.4).
