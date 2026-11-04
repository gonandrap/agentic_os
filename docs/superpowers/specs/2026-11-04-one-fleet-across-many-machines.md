# One fleet across many machines

Status: design proposal (investigation `wo-5515ff6b`). No code in this order. This
document is the input to a feature order.

## 0. The ask, and the short answer

> Each instance of the OS thinks it is the only instance. I want one master and
> slave instances drawing from the main database. Will SQLite support that? Can a
> local SQLite be a slave of a remote master, or does it need to be routed via the
> OS?

**Routed via the OS — but at the `ops` call, not at the row.** SQLite has no native
replication; every third-party option that gives a remote primary hands you read
replicas plus write-forwarding, which is less than this problem needs and more
machinery than it deserves. A perfect shared database would still not make a second
machine useful, because what is machine-bound in a Jarvis instance is not in the
database at all: the checkout, the worktrees, the `claude` sessions, and the daemon
that claims work.

Proposal: **one master instance owns all shared state and the scheduler; every other
machine is a runner that executes orders locally and speaks to the master over
HTTP.** The seam already exists — `ops.py` is the single business-logic chokepoint
that both `cli.py` and `ui/app.py` already go through.

## 1. The problem

A second machine cannot join an existing fleet. Everything the OS treats as durable
is keyed to one host's filesystem, and everything that decides what happens next
assumes it is the only thing deciding.

Evidence, from the code:

- **State is per-host by construction.** `paths.jarvis_home()`
  (`src/jarvis/paths.py:19`) is `$JARVIS_HOME`, default `~/.jarvis`; `os.db`
  (`paths.py:44`) and `neo.db` (`paths.py:48`) hang off it. Start the OS on a second
  machine and you get a second, empty knowledge base, backlog, inbox and Neo ledger.
  Nothing in either schema carries a host, an instance or an owner column.
- **A project's identity is an absolute local path.** `projects (name TEXT PRIMARY
  KEY, path TEXT NOT NULL, …)` in `src/jarvis/central_store.py`. The same project is
  `/Users/a/code/foo` on one machine and `/home/b/foo` on another, so the row cannot
  be shared as written.
- **The per-project database lives inside the checkout.**
  `paths.project_db_path()` (`paths.py:81`) = `<project>/.jarvis/jarvis.db`,
  gitignored. It is not a shared ledger that happens to sit in a repo; it is the
  state of *that copy* of the repo. Work orders, assumptions, approvals and events
  are all in it.
- **The scheduler is a singleton by assumption.** One pidfile
  (`paths.daemon_pidfile()`, `paths.py:61`), and `Daemon.dispatch_pending()` scans
  for pending orders and claims them with `project_store.claim_next_pending()`. Two
  daemons over one table both claim `wo-x`.
- **Execution is unavoidably local.** A worker's worktree is
  `project.path / ".claude" / "worktrees" / wo["id"]` (`src/jarvis/dispatch.py:104`);
  the worker is a local `claude` process spawned by `worker_session`; its transcript
  is read back off the local filesystem by `usage.read_session()`. Rows naming those
  artefacts are meaningless on any other host.
- **WAL rules out the filesystem shortcut.** `db.connect()` sets
  `journal_mode=WAL` (`src/jarvis/db.py:45`) on every store. WAL needs shared memory
  between processes on one host; SQLite's locking is documented as unreliable over
  network filesystems. `$JARVIS_HOME` on NFS, SMB, Dropbox or iCloud is not an
  option — it is a corruption path.

### 1.1 Root cause

**The OS has no concept of an instance.** Identity, ownership and scheduling are all
implicit in "the one host that is running". There is no row, no column and no
configuration anywhere that names which machine a record or a decision belongs to.
Replication is not a fix for that; it is a way to make two machines disagree faster.

The fix in §3 introduces that missing concept — a role (`master` or `runner`), a
named runner, and an owner for each claimed order — and leaves storage alone.

## 2. Why SQLite replication is the wrong tool

### 2.1 Does SQLite support a master/slave setup?

Not natively. SQLite is an in-process library over a file: no replication, no wire
protocol, no server. See §1 on WAL for why file sharing is not a substitute.

### 2.2 The third-party options, and what each would actually buy

- **LiteFS** (FUSE filesystem, leader/follower, write-forwarding proxy) is the closest
  match to "local SQLite as a slave of a remote master", and the one to rule out
  hardest: Fly.io lists it as **Not Supported**, the last release is v0.5.14 (April
  2025), and it is Linux/FUSE — it does not run on macOS, where this fleet lives.
- **Litestream.** Streaming backup to object storage, not replication to a live peer.
  The read-replica VFS described in October 2025 has not landed upstream. It gives
  durability, not a second writer. *Keep it* — §6.
- **libSQL / Turso embedded replicas.** This one genuinely works: a local SQLite file
  that reads locally and forwards writes to a remote primary, pulling on `sync()`.
  Cost: swap `sqlite3` for the libsql driver (one seam, `db.connect()`), run a
  self-hosted `sqld`, and give up interactive transactions —
  `db.write_transaction()` issues `BEGIN IMMEDIATE` (`db.py:61`) and the claim paths
  depend on it. Turso now steers new projects to its sync product instead.
- **rqlite.** Raft over SQLite, with its own HTTP API rather than a SQLite file. Every
  query in the three stores would be rewritten, statements must be deterministic, the
  underlying file must never be touched directly, and reads go to the leader by
  default. It replicates for fault tolerance, not for speed.

### 2.3 The argument that kills all four

Suppose the databases were perfectly shared. Then:

1. **Two daemons claim the same work** (§1), and spawn two workers in two different
   checkouts, on two different worktrees.
2. **The rows point at nothing.** A `wo_events` row written on machine B names a
   worktree, a session id and a turn file that exist only on machine B.
3. **Three streams, no transaction.** `os.db`, `neo.db` and each project DB would be
   three independent replication streams; nothing spanning them (an answer that
   writes a verdict *and* a learning *and* a work-order message) stays atomic.
4. **The gates and the merge queue are single-writer by design** — the
   privileged-action gate, the merge queue, the steward ledger and Morpheus all
   assume one arbiter.

Replication solves "two machines see the same bytes". The problem is "two machines
agree who is doing what" — scheduling, which belongs above the storage layer.

## 3. The fix: a master and its runners

```
          ┌──────────────────────── master (always on) ───────────────────────┐
          │  jarvisd: dispatch, Neo, gates, merge queue, steward, Morpheus    │
          │  os.db · neo.db · the order ledger · the knowledge base           │
          │  HTTP API (FastAPI, same app as the dashboard)                    │
          └───────────────▲────────────────────────────────▲──────────────────┘
                          │ HTTP over the private network  │
            ┌─────────────┴───────────┐         ┌──────────┴────────────┐
            │ runner: laptop A        │         │ runner: laptop B      │
            │ checkout + worktrees    │         │ checkout + worktrees  │
            │ claude CLI + creds      │         │ claude CLI + creds    │
            │ local exec state only   │         │ local exec state only │
            └─────────────────────────┘         └───────────────────────┘
```

**Mechanism.** The master owns every decision and every durable fact: the scheduler,
Neo, the gates, the knowledge base, the backlog, the inbox, the order ledger, the
dashboard. A runner owns nothing durable — it holds a checkout, spawns workers, and
streams what happened back. Wipe a runner and you lose a worktree, not a record.

**Where it lives, and why there.** In `ops.py`, because that is already the one
surface both `cli.py` and `ui/app.py` call; §4. The stores, the schema and the
`sqlite3` driver are untouched, which is the whole point of choosing this layer: the
change is a transport swap, not a storage migration.

**The CLI is the same binary in both roles.** `jarvis learn add`, `jarvis wo create`,
`jarvis status` on a runner are RPCs to the master; the command surface, the output
and the operator's habits do not change.

### 3.1 Roles and configuration

| Variable | Master | Runner |
|---|---|---|
| `JARVIS_ROLE` | `master` | `runner` |
| `JARVIS_MASTER_URL` | unset | `https://<master>:8788` |
| `JARVIS_RUNNER_TOKEN` | — | the shared secret |
| `JARVIS_RUNNER_NAME` | — | stable machine name (`gonzalo-mbp`) |

Default with both unset: today's behaviour, byte for byte. A single-machine fleet
never learns any of this exists.

### 3.2 Project identity becomes (project, runner)

A new `project_runners` table on the master: `(project, runner, path, status,
last_seen)`. `projects.path` stops being *the* path and becomes the master's own
copy, or null when the master holds no checkout. A runner registers its local path
for each project it can serve; the scheduler only offers `wo-x` in project `foo` to a
runner that has registered `foo`.

### 3.3 The scheduler, and how a runner gets work

Claiming moves to the master and becomes a request, not a scan:

1. Runner polls `POST /api/runner/claim {runner, projects[]}`.
2. Master runs today's `claim_next_pending` logic inside one `BEGIN IMMEDIATE`,
   stamps `work_orders.assigned_runner`, and returns the dispatch payload — the
   prompt `dispatch.build_worker_prompt` already composes, the resolved model, the
   effort, the permission mode, the worker settings.
3. Runner spawns the worker locally through `worker_session`, exactly as today.
4. Runner reports turns, events and status transitions back over
   `POST /api/runner/report`.

A claimed order is pinned to its runner for its life: the worktree and the Claude
session cannot move. A runner that stops reporting for `N` minutes marks its orders
stalled through the existing supervisor path — the health probes and the `INV-*`
invariants need a new symptom, not a new concept.

### 3.4 Hooks: the latency question, answered locally

`hooks.handle_hook()` runs on **every tool call** inside a worker. Routing it to the
master would put a network round trip in front of every `Bash` a worker makes, and a
master hiccup would block the worker rather than slow it.

So **hooks stay local and synchronous.** The runner keeps a local
`<project>/.jarvis/jarvis.db` holding exactly the execution rows — `wo_events`,
`wo_turns`, the hook's own preflight state. Of the two decisions a hook must make:

- The **PR-title rule** is pure string matching against the work-order id. No I/O.
- **Gates** need the master. Latency is acceptable there and correctness is not
  negotiable: a gate decision is rare, it is already a wait, and one that failed open
  on a network error would be a security hole. A gate request that cannot reach the
  master **denies**, and says why.

Everything else a hook writes is a report, and reports batch: the runner flushes
events to the master on its report tick.

This is deliberately a split, not a clean rule — the per-project database is divided
into a ledger the master owns and execution rows the runner keeps. The alternative,
routing every hook write, trades a worker-blocking failure mode for tidiness.

### 3.5 What crosses the wire, and what does not

| Call | Where it runs |
|---|---|
| `jarvis learn add / search / show`, `backlog`, `inbox`, `status`, `wo create/list/show/send/review/ack/done`, `fo`, `io`, `do`, `mo`, `gate`, `neo`, `cost`, `search` | master, via RPC |
| `jarvis wo ask` (a worker's question to Neo) | master, via RPC |
| gate preflight for a privileged action | master, via RPC; **deny on failure** |
| hook event recording, turn capture, worker spawn, worktree creation, git | runner, locally |
| `jarvis ui` | master only |

## 4. The seam in the code

`ops.py` is already the one place both `cli.py` and `ui/app.py` call for business
logic, which makes the client/server split a transport swap rather than a rewrite:

- **Server**: expose the ops surface under `/api/v1/...` on the existing FastAPI app
  (`ui/app.py` already serves `/api/...` routes and already delegates its POST
  actions to `ops`). JSON in, JSON out, shaped by the dataclasses `ops` returns.
- **Client**: a `remote.py` module mirroring the ops function signatures over HTTP.
- **Dispatcher**: `cli.py` resolves `ops` or `remote` once per invocation from
  `JARVIS_ROLE`. Every `jarvis` import in `cli.py` is already lazy and inside its
  handler, so this is one resolution helper, not 60 edited call sites.

The property to hold onto: **one ops surface, two transports.** A command that grows
a new ops function gets its remote form for free when the shape is a dataclass and
the error is an `OpsError`.

## 5. Transport and trust

- **Network**: a private mesh (Tailscale or equivalent), not the public internet. The
  master binds to its mesh address; nothing is port-forwarded.
- **Auth**: a per-runner bearer token, issued by `jarvis runner add <name>`, stored in
  `os_state`, revocable with `jarvis runner revoke`. It authorises *runner*
  operations; it is not an operator credential.
- **TLS**: terminated by the mesh. No certificate management in Jarvis.
- **Refusal to guess**: a runner that cannot reach the master does not fall back to
  local writes. It reports the outage and stops claiming. Split-brain is the one
  failure this design must never produce, and a local write queue is how you get it.

## 6. Durability

The master becomes a single point of failure for state previously spread across
machines — the point of the design, but it raises the stakes on backup. This is where
Litestream earns its place: continuous streaming of `os.db` and `neo.db` to object
storage, restore to a point in time, no second writer and no claim about replication.
It answers "the master's disk died", which is the real risk, rather than "two machines
must write at once", which this design has removed.

## 7. Phasing — three shippable steps

**Phase 1 — read-only remote.** `JARVIS_ROLE=runner` serves the read commands from
the master: `status`, `learn search/show/list/topics`, `backlog list`, `inbox`, `wo
list/show`, `search`. No scheduler change, no runner registration, no execution
moves. After this phase a second machine can *see* the fleet, which is most of the
daily friction. Rollback is an unset environment variable.

**Phase 2 — remote writes.** The mutating ops surface: `wo create/send/review/ack/
done`, `fo`/`io`/`do`/`mo` creation and review, `learn add`, `backlog add/promote`,
`bug report`, `gate approve/deny`. After this phase both machines are full operator
consoles; work still executes only on the master.

**Phase 3 — runners execute.** `project_runners`, `assigned_runner`, the claim/report
protocol, local hooks with remote gates, the stalled-runner symptom. This is where
the development speed-up the order asks for actually arrives.

Phases 1 and 2 are mechanical and low-risk. Phase 3 is the feature. Splitting them
means the fleet is useful from two machines long before the scheduler changes.

## 8. What this does not solve, and what to decide before building

- **The master must be always on.** A laptop that sleeps is not a master. Either a
  small always-on box (the production Mac this fleet already runs on) or a VPS.
  Naming that machine is the first decision of the feature order.
- **Credentials do not replicate.** Each runner needs its own `claude` auth, its own
  `gh` auth and its own MCP servers. `jarvis config wiring` becomes per-runner.
- **A runner's checkout can be stale or dirty.** The claim protocol should carry the
  base SHA the master expects, and a runner whose checkout cannot reach it refuses the
  claim rather than working from the wrong tree.
- **Cost accounting crosses machines.** `usage.read_session()` reads Claude Code
  transcripts off the local filesystem; with workers on runners, the bill must be
  assembled from what the runner reports, not from what the master can read. The most
  under-specified corner of Phase 3; it deserves its own section in the feature plan.
- **Schema fence.** `docs/superpowers/specs/2026-11-01-os-db-schema-fence.md` governs
  changes to `os.db`; `project_runners` and `assigned_runner` must land through it.

## 9. Recommendation in one line

Do not replicate SQLite. Make one instance the master, make the others runners that
call it through the ops surface it already has, and split the per-project database
into a ledger the master owns and execution rows the runner keeps.
