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
| `JARVIS_RUNNER_TOKEN` | — | the machine's token (§5.1) |
| `JARVIS_RUNNER_NAME` | — | stable machine name (`gonzalo-mbp`) |

The operator credentials are **not** in this table and are not environment variables:
the session token is `$JARVIS_HOME/credentials.json`, and the key that authorises
decisions is in the Keychain and cannot be put in a variable at all (§5.1). Neither
placement is what keeps a worker out — see §3.3.

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

**`JARVIS_RUNNER_TOKEN` is withheld from the environment of every worker the runner
spawns.** This is a change, not a property the code already has: a worker **inherits
the spawning process's environment today**. the background worker spawn builds it as
`{k: v for k, v in os.environ.items() if k not in CACHE_TTL_KEYS}`
(`claude_cli.py:843`) — a copy of the whole environment minus one named set — and
`_run`, the synchronous path, takes a bare `os.environ.copy()` (`claude_cli.py:178`).
The env map in `dispatch.py:193` is the *settings* env, which only adds. So a runner
token in the runner service's environment reaches every worker unless it is stripped.

Where it lives: `CACHE_TTL_KEYS` (`claude_cli.py:47`, filtered at `:843`) is already
the "variables a spawned process must not inherit" set and is the natural home for
the runner token; `_run` needs the same filter rather than a bare copy. The feature
must apply one excluded set on both paths and pin it with a test asserting the
variable is absent from a spawned worker's environment.

Why it matters: a worker that could read `JARVIS_RUNNER_TOKEN` could call the runner
API as its own runner — claim other orders, report false status.

What this strip is **not**: a barrier against a worker reaching the *decision* API.
A worker runs as the same OS user on the same machine, so anything at rest on that
machine — an environment variable, or a 0600 file — is readable by it; moving a secret
out of the environment raises the cost of reading it, nothing more (§5.1). What keeps
a worker away from `gate approve/deny` is the presence key of §5.1: the master refuses
every decision call that does not carry a signature over a fresh master-issued
challenge, produced by a Keychain key whose use requires a human at the machine. A
worker has no tty (`kn-e97f6b51`) and cannot answer that prompt.

**The strip leaves two calls without a path, and they need one.** A worker legitimately
makes exactly two calls to the master: `jarvis wo ask`, and the gate preflight that
`hooks.handle_hook` runs in the worker's own environment (`kn-ec8463f1`). Both were
specified as runner-token calls, and §3.3 takes that token away. Left there, every
Phase 3 preflight fails closed (§3.4) and no worker can reach Neo — and the fix an
implementer reaches for first is to hand the token back, which restores exactly the
harm the strip exists to prevent. The path is §3.6, and it is part of the strip, not a
follow-up to it.

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
  master **denies**, and says why. The hook does not reach the master directly — it
  has no credential to do so (§3.3) — it goes through the runner's forwarder (§3.6),
  and a forwarder that is down is the same thing as a master that is unreachable:
  deny.

Everything else a hook writes is a report, and reports batch: the runner flushes
events to the master on its report tick.

This is deliberately a split, not a clean rule — the per-project database is divided
into a ledger the master owns and execution rows the runner keeps. The alternative,
routing every hook write, trades a worker-blocking failure mode for tidiness.

### 3.5 What crosses the wire, and what does not

| Call | Where it runs | Credential (§5) |
|---|---|---|
| `jarvis learn search/show`, `backlog list`, `inbox`, `status`, `wo list/show`, `cost`, `search`, `gate list/show` | master, via RPC | operator session |
| `jarvis learn add`, `backlog add/promote`, `wo create/send`, `fo`/`io`/`do`/`mo` creation, `bug report` | master, via RPC | operator session |
| **`jarvis gate approve/deny`, `wo review/ack/done`, `fo approve`, `io review`, `neo answer/review/retract`, `learn retract`, `config set`** | master, via RPC | **operator presence key — a runner token is refused, and so is a session token alone** |
| `jarvis wo ask` (a worker's question to Neo) | worker → runner forwarder → master (§3.6) | **per-order scoped token, added by the runner; the worker holds none** |
| gate preflight for a privileged action — *asking*, never deciding | worker → runner forwarder → master (§3.6); **deny on failure** | **per-order scoped token, added by the runner; the worker holds none** |
| runner claim and report | master, via RPC | runner token — **a scoped token is refused** |
| hook event recording, turn capture, worker spawn, worktree creation, git | runner, locally | none |
| `jarvis ui` | master only | operator |

The third row is the line that matters: every call that **decides something on the
user's behalf** needs a signature from the presence key, and the master refuses it
when the caller presents only a runner token or only a session token. The first two
rows ride a bearer token that a worker on the runner can read; §5.1 states that
plainly and bounds what it is worth.

### 3.6 How a worker reaches the master without a credential

A worker holds no token at all. `JARVIS_RUNNER_TOKEN` is stripped (§3.3), and the
operator credentials are the user's, not the machine's. Its two legitimate calls go
through a **forwarder the runner owns**:

- The runner listens on a **unix socket** it creates at spawn time, one per claimed
  order, and passes only its **path** to the worker (`JARVIS_RUNNER_SOCKET`). A path
  is not a credential: holding it grants nothing the forwarder does not choose to do.
- The socket accepts exactly **two verbs**: `ask` and `gate_preflight`. Anything
  else — claim, report, any ops call, any passthrough or "forward this request" verb —
  is refused by the forwarder and not forwarded. **The forwarder has no general
  proxy mode;** that is the whole point of it being a forwarder rather than a tunnel.
- The runner attaches the credential itself. The request the master sees carries a
  **per-order scoped token** the master minted at claim time (§3.3), bound to that
  order's id, valid only for `ask` and `gate_preflight`, and expiring when the order
  reaches a terminal state. **The master refuses claim and report presented with a
  scoped token**, and refuses a scoped token for an order id other than the one it
  was minted for. That refusal is server-side, so it holds even if the forwarder is
  wrong or is replaced.
- **The runner token itself never reaches a worker** — not in its environment, not in
  a file in its worktree, and not in a forwarder response. A worker never sees a
  credential of any kind.

Why both halves. The socket is what keeps a secret out of the worker; the scoped token
is what makes the restriction real at the master instead of a promise by the runner. A
forwarder that simply held the runner token and filtered verbs would put the whole
runner identity behind one local bug.

**The residual risk, stated.** Every worker on a runner is the same OS user, so one
worker can connect to another order's socket and ask Neo a question, or raise a
preflight, in that order's name. The scoped token bounds the damage to the two verbs:
no claiming, no reporting, no decision. The loss is attribution between orders on one
machine, not authority — and a preflight raised under the wrong order id still ends at
a human or Neo deciding the gate. Narrowing it further means per-worker authentication
to its own runner, which is the same same-user problem as §5.1 and is not worth the
machinery for an ask.

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
- **TLS**: terminated by the mesh. No certificate management in Jarvis.
- **Refusal to guess**: a runner that cannot reach the master does not fall back to
  local writes. It reports the outage and stops claiming. Split-brain is the one
  failure this design must never produce, and a local write queue is how you get it.

### 5.1 Four credentials, because file permissions are not a barrier against a worker

**Start from what does not work.** Review round 1 produced a two-credential design
whose operator credential was a bearer token in `$JARVIS_HOME/credentials.json` at
mode 0600, and the spec claimed that being a file rather than an environment variable
kept it away from workers. That claim was wrong and must not reach the feature order.
**A worker runs as the same OS user as the runner that spawned it, on the same
machine. Mode 0600 grants that user read access. `cat ~/.jarvis/credentials.json`
succeeds, and so does running `jarvis gate approve` directly.** File mode separates
users; it does not separate a user's processes from each other. Withholding a secret
from the environment (§3.3) raises the cost of reading it from zero to one `cat` — it
is worth doing, and it is not a security boundary.

So a credential a worker cannot use must be one the worker cannot *obtain*, no matter
what it reads. That rules out every secret sitting at rest on the runner, and leaves
three surfaces:

| | Runner token | Operator session | Operator presence key |
|---|---|---|---|
| Identifies | a machine | a logged-in human on a machine | a human, **now** |
| Issued by | `jarvis runner add <name>` on the master | `jarvis login`, one per (human, machine) | `jarvis login --enrol`, same moment |
| Held as | `JARVIS_RUNNER_TOKEN`, stripped from workers (§3.3) | `$JARVIS_HOME/credentials.json`, mode 0600 | a private key in the macOS Keychain, created with `kSecAccessControlUserPresence`; the key material never leaves the Secure Enclave and every use raises a Touch ID / password prompt |
| Readable by a worker | no, once stripped | **yes — assume it is compromised** | **no: the key cannot be exported, and a use needs a human at the machine** |
| Authorises | claim and report, and nothing else — **not `wo ask`, not gate preflight**, which take the forwarder and a scoped token (§3.6) | reads, and writes that only *record* something | every **decision** row of §3.5 |
| Revoked by | `jarvis runner revoke <name>` | `jarvis logout --runner <name>` | `jarvis operator revoke <name>` — the master forgets the public key |
| Stored on master as | a hash in `os_state` | a hash in `os_state`, with issue time and last use | the enrolled **public** key, with issue time and last use |

**How a decision is authenticated.** Every operator-only call of §3.5 is a two-step
exchange, and the master is the one enforcing it:

1. The client asks the master for a challenge: a fresh random nonce, bound to the
   exact call (verb, target id, and for a gate the decision and the reason), with a
   short expiry and single use.
2. The client signs that challenge with the presence key. The Keychain raises the
   prompt; the signature is produced inside the Secure Enclave.
3. The master verifies the signature against the public key enrolled for that
   operator, checks the nonce is unexpired, unused and bound to this exact call, and
   only then executes.

**The rule the implementation must enforce:** *the master refuses `gate
approve/deny` — and every other row marked operator-only in §3.5 — unless the request
carries a valid signature over a fresh master-issued challenge from an enrolled
operator key. A runner token is refused. An operator session bearer token alone is
refused.* The refusal is `403`, it names what was missing, and it is recorded as a
gate event rather than silently dropped, because a worker reaching for an approval
endpoint is a signal the user wants to see.

**Why a worker cannot satisfy this.** It is not that the worker is denied the files —
it may read every byte of `credentials.json` and still get nowhere, because the thing
it needs is a signature from a key that is not in any file and whose use requires a
human present at that machine. A worker has no tty (`kn-e97f6b51`), no window server
session it controls, and no finger. Binding the nonce to the call also means a
signature the user produced for one approval cannot be replayed for another.

**No session caching, deliberately.** Each operator-only call takes its own presence
prompt. A "remember me for 10 minutes" window is a token at rest again, and a worker
polling the gate endpoint would eventually land inside one. Decisions are rare —
gates, reviews, approvals — so the cost is a Touch ID tap per decision.

**What the operator session still buys, and what it costs.** The bearer token in
`credentials.json` remains, because reads and recording writes cannot each ask for a
fingerprint. It is explicitly **not** worker-proof, and the spec assumes a worker can
read it. The blast radius is bounded and must be stated in the feature order: a worker
holding it can read anything the fleet knows, and can create records — `learn add`,
`backlog add`, `wo create`, `bug report`. Those decide nothing; they produce rows the
user sees and can retract. Every call that decides, approves, reviews, retracts or
reconfigures is behind the presence key.

**Portability.** `kSecAccessControlUserPresence` is macOS/iOS. This fleet's master and
runners are Macs, so it is the mechanism. On a platform without it, the equivalent
with the same property is to hold the key as a **different OS user** than the one
workers run as, reachable only over a unix socket whose directory that user owns —
file mode works there, because it is then separating two users rather than one user's
processes. A platform offering neither falls back to §5.2: decisions on the master's
UI only. What must never ship is a decision authenticated by a secret a worker can
read.

The operator credentials are deliberately **not** derived from the runner's identity:
a runner that is fully compromised can still only claim, report, read and record.

**A fourth credential, and it is the weakest on purpose: the per-order scoped token**
of §3.6. The runner holds it, never the worker; it authorises `ask` and
`gate_preflight` for one order id and nothing else; the master refuses claim and
report presented with it, and refuses it for any other order. It exists so that a
worker needs no credential at all, and so that the restriction is enforced where it
can be trusted — at the master — rather than by the forwarder being correct.

### 5.2 The alternative, and why it is the fallback rather than the design

Approvals could instead be restricted to the master's dashboard — no operator
credential, no remote decision, and the user walks to the master (or port-forwards
its UI over the mesh) to approve anything. That is strictly safer and strictly less
useful: the whole point of the order is to stop one machine being the only place the
fleet can be operated from.

It is worth stating because it is the correct fallback if Phase 2 slips. **If the
presence key is not built, the decision rows of §3.5 stay on the master's UI and are
not exposed over RPC at all.** What must never happen is the third option — exposing
them over RPC behind a secret that lives on the runner's disk, which is what the
runner token and the operator session both are.

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

Phase 1 is not credential-free: those reads are the operator-session rows of §3.5, so
**`jarvis login` and the session token ship in Phase 1**, with the master authenticating
every read against it. The presence key is a Phase 2 concern because Phase 1 exposes no
decision. What Phase 1 must not do is accept an unauthenticated read "because it is only
a read": the fleet's state is on a mesh, not a private socket.

**Phase 2 — remote writes.** The mutating ops surface: `wo create/send/review/ack/
done`, `fo`/`io`/`do`/`mo` creation and review, `learn add`, `backlog add/promote`,
`bug report`, `gate approve/deny`. After this phase both machines are full operator
consoles; work still executes only on the master.

**Phase 2 has a prerequisite, not an option: the presence key of §5.1 — enrolment,
the challenge endpoint and signature verification — ships first, in the same phase and
before any decision row of §3.5 is exposed.** The
decision rows (`gate approve/deny`, `wo review/ack/done`, `fo approve`, `io review`,
`neo answer/review/retract`, `config set`) are the reason the presence key exists; a
Phase 2 that shipped them against the runner token — or against the session token,
which a worker can read — would be a worker able to approve its own privileged action.
If the presence key slips, those rows stay on the master's UI (§5.2) and Phase 2 ships
without them; the recording writes may still ship on the session token.

**Phase 3 — runners execute.** `project_runners`, `assigned_runner`, the claim/report
protocol, local hooks with remote gates, the forwarder and the per-order scoped token
of §3.6, the stalled-runner symptom. This is where the development speed-up the order
asks for actually arrives.

Phase 3 does not ship without a test that **a worker going through the forwarder
cannot claim or report**: it asserts that the forwarder refuses every verb but `ask`
and `gate_preflight`, and that the master refuses a claim and a report presented with
a per-order scoped token. The companion test from §3.3 — `JARVIS_RUNNER_TOKEN` absent
from a spawned worker's environment — is what makes the first one meaningful, so they
land together.

Phase 1 is mechanical. Phase 2 is not: the presence key is the one piece of this
design with no precedent in the codebase, and it should be built and tested on its own
before the write surface depends on it. Phase 3 is the feature. Splitting them
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
- **The presence key needs a dependency decision.** The core is stdlib-only. A
  Secure-Enclave key gated on user presence is reachable either by shelling out to
  `/usr/bin/security` (stdlib, but `security` cannot create a `kSecAccessControlUser`
  `Presence` key — it would have to be enrolled by a small helper) or by `ctypes` into
  `Security.framework` (stdlib, more code, more ways to be wrong). Decide which in the
  feature plan; the spec's requirement is the property, not the binding.
- **Who the operators are.** §5.1 issues one session and one presence key per (human,
  machine). If the
  fleet ever has more than one operator, the master must also record *which* operator
  decided a gate — today a decision has a timestamp and a reason, not an author.
  Out of scope here; name it in the feature plan before the credential ships.
- **Schema fence.** `docs/superpowers/specs/2026-11-01-os-db-schema-fence.md` governs
  changes to `os.db`; `project_runners` and `assigned_runner` must land through it.

## 9. Recommendation in one line

Do not replicate SQLite. Make one instance the master, make the others runners that
call it through the ops surface it already has, and split the per-project database
into a ledger the master owns and execution rows the runner keeps.
