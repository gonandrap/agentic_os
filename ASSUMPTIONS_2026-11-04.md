# Assumptions — wo-5515ff6b (investigate multi-machine OS)

1. **Deliverable is a spec document, not code.** The order says its output feeds a
   feature order, so it lands as
   `docs/superpowers/specs/2026-11-04-one-fleet-across-many-machines.md`, the path
   CLAUDE.md names for design docs. Rejected: a knowledge-base entry only (too small
   to carry an architecture) and an implementation (explicitly out of scope).

2. **Recommendation is OS-level RPC, not storage replication.** Rejected:
   LiteFS (unsupported by Fly.io, Linux/FUSE only, no macOS), Litestream (backup, not
   live replication), libSQL embedded replicas (works, but costs the `sqlite3`
   driver, a self-hosted `sqld` and interactive transactions) and rqlite (rewrites
   every query, deterministic-SQL constraint). Reason: none addresses the actual
   blocker, which is that two daemons over one table both claim the same work order
   and run it in two different checkouts.

3. **The per-project database is split rather than moved.** The ledger goes to the
   master; `wo_events`/`wo_turns` and hook preflight stay on the runner, because
   `hooks.handle_hook` runs on every tool call and a round trip there would block
   workers on master latency. Rejected: routing every hook write to the master.

4. **Gates fail closed.** A gate preflight that cannot reach the master denies rather
   than allowing locally. Rejected: a local fallback grant, which is a security hole.

5. **Two credentials, not one (review round 1).** A runner token identifies a machine;
   a separate operator credential identifies the user and is required for every call
   that decides on their behalf — `gate approve/deny`, `wo review/ack/done`,
   `fo approve`, `io review`, `neo answer/review/retract`, `config set`. The master
   refuses those when the caller presents only a runner token. The operator credential
   is a file (`$JARVIS_HOME/credentials.json`, 0600), never an environment variable.
   Rejected: one scoped token (a worker inherits the runner's environment, so it could
   approve the gate it is asking for); approvals on the master's UI only (safer, but it
   is the fallback in §5.2, not the design, since remote operation is the point of the
   order).

6. **Decisions are authorised by a user-presence key, not by a stored secret (review
   round 2).** A worker runs as the same OS user as its runner, so a 0600
   `credentials.json` is readable by it and is not a barrier. Every operator-only call
   is signed with a macOS Keychain key created with `kSecAccessControlUserPresence`
   (Touch ID / password), over a fresh master-issued nonce bound to that exact call;
   the master refuses the call without that signature. Rejected: a tty prompt alone (a
   worker can skip the CLI and call the API with the stored token); a cached presence
   session (a token at rest again). Portable variant where there is no Keychain: hold
   the key as a different OS user reachable over a unix socket. Accepted residual risk:
   the session bearer token for reads and recording writes IS worker-readable.

7. **Phasing is three steps, read-only first.** Rejected: one big change, because
   phases 1 and 2 give a second operator console with no scheduler change and roll
   back by unsetting an environment variable.
