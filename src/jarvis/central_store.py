"""Central store: $JARVIS_HOME/os.db

Holds everything that must be unified across projects: the project registry, the
notification inbox, the backlog (with dependencies), and the knowledge base.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from . import db
from .paths import central_db_path, ensure_home

#: How old a base-health reading may be before the merge pause stops acting on it — ~7
#: PR-poll intervals. A `gh` that stops answering must not pause the fleet's merges for
#: ever on a fact nobody can confirm, and 900s is several polls, so one transient failure
#: does not resume merging onto a red default branch either (spec
#: docs/superpowers/specs/2026-09-26-a-red-default-branch-raises-itself.md §2).
BASE_HEALTH_FRESH_SECONDS = 900


def base_health_key(project: str) -> str:
    """The `os_state` key holding one project's default-branch health."""
    return f"base_health:{project}"


# Tag marking knowledge mirrored out of a Claude Code memory file rather than typed
# by a worker via `jarvis learn add`.
MEMORY_TAG = "claude-memory"

# Tag marking knowledge that is injected into every worker prompt in full, instead of
# only as a headline in the index. Reserved for safety rails a worker must not be able
# to miss by failing to search.
PINNED_TAG = "pinned"

# How much of an entry survives into the index line. Long enough for a full short
# learning (most are one sentence), short enough that 40 of them cost ~1.5k tokens.
HEADLINE_CHARS = 160

# Read verbs that AIM at an entry, as against the two that sweep the index. Only these
# record per-entry hits: `list` returns everything, so counting it would mark the whole
# base as consulted and destroy the one number that says which entries earn their place.
AIMED_VERBS = ("show", "search")

#: How many words a work-order title has to carry for a search built out of it to mean
#: anything. Below this the query is words like "fix the", which match a large share of the
#: base — enough to manufacture a title "match" for every short title. Used twice, by
#: `knowledge_brief`'s hint tier and by `ops.knowledge_usage_report`'s `could_have_read`;
#: it lives here because the store cannot import `ops` (layering runs stores upward only).
MISSED_MIN_WORDS = 3


def split_tags(tags: str) -> list[str]:
    return [t for t in (s.strip() for s in (tags or "").split(",")) if t]


def has_tag(tags: str, tag: str) -> bool:
    return tag in split_tags(tags)


def fts_query(term: str) -> str:
    """A user's words as an FTS5 OR-query. '' when nothing in them is searchable.

    Each word becomes a quoted phrase, so `-`, `:` and `"` reach the tokenizer as text
    instead of as FTS5 syntax — see
    docs/superpowers/specs/2026-08-24-ranked-knowledge-search.md §5.
    """
    words = db.cap_words([w for w in (term or "").split() if any(c.isalnum() for c in w)])
    return " OR ".join('"' + w.replace('"', '""') + '"' for w in words)


def headline(content: str, limit: int = HEADLINE_CHARS) -> str:
    """One-line gist of an entry: its first line, truncated.

    Mirrored memory files are whole documents; taking the first line keeps a 4 KB entry
    from costing 4 KB in an index whose entire point is to be cheap.
    """
    text = " ".join((content or "").strip().split("\n", 1)[0].split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


@dataclass
class KnowledgeBrief:
    """What a worker prompt says about the knowledge base.

    Deliberately *not* the knowledge itself: `pinned` carries full text for the few
    entries that were curated as unmissable, `digest` carries headlines + ids so the
    worker can fetch what it needs, and `overflow` names the topics that did not fit
    so nothing is silently invisible.

    `hints` is the one relevance-driven tier: entries whose text matches the work order's
    own TITLE, which is how an entry sitting in `overflow` gets pointed at instead of
    waiting for the worker to guess a search term.
    """
    project: str
    total: int = 0
    pinned: list[dict[str, Any]] = field(default_factory=list)
    digest: list[dict[str, Any]] = field(default_factory=list)
    overflow: list[tuple[str, int]] = field(default_factory=list)
    hints: list[dict[str, Any]] = field(default_factory=list)

    @property
    def overflow_count(self) -> int:
        return sum(n for _, n in self.overflow)

    def __bool__(self) -> bool:
        return self.total > 0

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    name TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    model TEXT,
    status TEXT NOT NULL DEFAULT 'active',  -- active | stopped
    last_seen REAL,
    catalog_json TEXT
);
CREATE TABLE IF NOT EXISTS inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    project TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    wo_id TEXT,
    status TEXT NOT NULL DEFAULT 'new',     -- new | notified | acked
    sink_results TEXT
);
CREATE TABLE IF NOT EXISTS backlog (
    id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',    -- open | promoted | done | dropped
    depends_on TEXT NOT NULL DEFAULT '[]',  -- JSON list of backlog ids
    promoted_wo_id TEXT,
    created_at REAL NOT NULL,
    -- Where this item came from. NULL/'' on anything a human typed; filled in when a
    -- work order deferred it, whoever ends up filing the row (see ADDED_COLUMNS).
    origin_wo_id TEXT,                      -- the work order that suggested it
    origin_fo_id TEXT,                      -- the feature order whose plan it came from
    origin_note TEXT NOT NULL DEFAULT ''    -- the why, and the Neo question id if given
);
CREATE TABLE IF NOT EXISTS knowledge (
    id TEXT PRIMARY KEY,
    project TEXT NOT NULL DEFAULT '',       -- '' = global
    ts REAL NOT NULL,
    topic TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '',
    retired_at REAL,                        -- NULL = standing; set = superseded
    retired_reason TEXT,                    -- why, in the user's words
    -- WHO WROTE IT, and who retired it. Reads were attributed from the day
    -- `knowledge_reads` existed and writes were not, which is how a work order whose
    -- whole deliverable was a retraction reached the validation panel looking like it
    -- had done nothing at all (issue #200, spec 2026-09-12 §4). '' on every
    -- pre-existing row and on anything a person typed at a terminal.
    wo_id TEXT NOT NULL DEFAULT '',         -- the work order that ADDED this entry
    retired_by_wo_id TEXT NOT NULL DEFAULT ''  -- the work order that RETIRED it
);
CREATE TABLE IF NOT EXISTS os_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
-- One retrieval FROM the knowledge base, recorded where it happens.
--
-- Until this table existed the OS could say what it KNEW and nothing at all about what
-- was READ: whether workers consult the base, which entries earn their place, which are
-- dead weight, and which questions it is asked and cannot answer. The only evidence was
-- an opt-in paid eval somebody had to remember to run. See
-- docs/superpowers/specs/2026-08-23-what-memory-costs-and-who-reads-it.md.
--
-- Recorded at the read for the same reason `agent_calls` is recorded at the call: a
-- `jarvis learn show` leaves no trace anywhere else, so nothing can recover it later.
--
-- `chars` is what the read COST — content characters handed back — which is the only
-- honest measure of the knowledge base's share of a worker's context. The index in the
-- prompt is a fixed budget; this is the variable part.
CREATE TABLE IF NOT EXISTS knowledge_reads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    project TEXT NOT NULL DEFAULT '',
    wo_id TEXT NOT NULL DEFAULT '',         -- '' = no work order: a person at a terminal
    verb TEXT NOT NULL,                     -- show | search | list | topics
    term TEXT NOT NULL DEFAULT '',          -- the query, or the ids asked for
    hits INTEGER NOT NULL DEFAULT 0,        -- rows returned
    chars INTEGER NOT NULL DEFAULT 0        -- content characters returned
);
-- Which entries a read actually returned, so "how often was THIS consulted" and "what
-- has never been read" are one GROUP BY rather than a scan of `term` strings.
CREATE TABLE IF NOT EXISTS knowledge_read_hits (
    read_id INTEGER NOT NULL REFERENCES knowledge_reads(id) ON DELETE CASCADE,
    kn_id TEXT NOT NULL
);
-- One Claude call the OS made on its OWN behalf, and the work order it was made for.
--
-- A work order's spend is not just its worker's turns: every question it asked Neo, every
-- seat of the panel that deliberated on it, every digest written for the dashboard is a
-- `claude -p` call Jarvis paid for BECAUSE of that work order. Those calls have no session
-- Jarvis owns and no transcript it can attribute, so unlike worker turns they cannot be
-- recovered after the fact — recording them at the moment they happen is the only way they
-- are ever counted. `usage.py`'s opening line ("Jarvis records no token usage of its own")
-- stopped being true here.
--
-- Central rather than per-project or in `neo.db`: Neo, the panel and the digest are three
-- subsystems and future OS calls will be a fourth, and os.db is the store that already
-- unifies across projects and already carries the work order's purge path.
--
-- Token classes are columns AND `usage_json` on purpose: the columns are what the fleet
-- report sums in SQL over every work order at once, and the JSON keeps the full envelope
-- (the ephemeral 1h/5m split, the per-call context peak) for anyone reading one call.
CREATE TABLE IF NOT EXISTS agent_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    project TEXT NOT NULL DEFAULT '',
    wo_id TEXT NOT NULL DEFAULT '',         -- '' = OS work no work order caused
    kind TEXT NOT NULL,                     -- neo_answer | panel_seat | digest | ...
    label TEXT NOT NULL DEFAULT '',         -- the seat name, or whatever names the call
    model TEXT NOT NULL DEFAULT '',
    question_id INTEGER,                    -- the neo.db question, where there is one
    -- The session the CLI minted for this one-shot call. Stored so an OS call can be
    -- opened up per API CALL the same way a worker turn now can: its transcript is the
    -- only place that detail exists, and this id is the only handle on it. Recorded at
    -- the call because nothing can recover it afterwards — the same reason the token
    -- counts beside it are.
    session_id TEXT NOT NULL DEFAULT '',
    ok INTEGER NOT NULL DEFAULT 1,
    cost_usd REAL,                          -- the CLI's own figure — exact, not a proxy
    input INTEGER NOT NULL DEFAULT 0,
    cache_write INTEGER NOT NULL DEFAULT 0,
    cache_read INTEGER NOT NULL DEFAULT 0,
    output INTEGER NOT NULL DEFAULT 0,
    -- How big the OS's own input to this call was, in characters. Measurement only,
    -- nothing is capped: spec §3,
    -- docs/superpowers/specs/2026-09-26-bounded-model-inputs.md.
    prompt_chars INTEGER NOT NULL DEFAULT 0,
    system_prompt_chars INTEGER NOT NULL DEFAULT 0,
    -- How long the call took. NULLABLE — also in ADDED_COLUMNS, where the reasoning is.
    latency_ms INTEGER,
    usage_json TEXT
);
-- What the OS believes is a privileged action, and what it has LEARNED is not.
--
-- The recognisers behind the gates used to be regex tuples in `gates.KINDS`, and the
-- problem with that was not the regexes — it was that the table had no writer. Every
-- false positive was reviewed by Neo, correctly identified, dismissed, and forgotten;
-- the next work order in the next project tripped the same gate on the same shape. The
-- only path from "the reviewer knows this is harmless" to "the OS stops asking" ran
-- through a human filing a work order to widen a pattern.
--
-- Central rather than per-project, and that is the whole point rather than a filing
-- decision: a dismissal in one project has to settle the question for the next one.
-- The `project`/`wo_id`/`approval_id` columns are provenance — where this was learned —
-- not scope.
--
-- Three roles, and the third is what makes the first two safe to change: `match`
-- recognises an attempt, `exempt` clears a mention, and `canary` is a command that must
-- always gate, which every proposed exemption is tested against before it may exist.
-- See gate_rules.py.
CREATE TABLE IF NOT EXISTS gate_rules (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    role TEXT NOT NULL,                     -- match | exempt | canary
    kind TEXT NOT NULL DEFAULT '',          -- gate name; '' on an exemption = every gate
    test TEXT NOT NULL,                     -- regex | signature | command
    pattern TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'builtin', -- builtin | neo | user
    project TEXT NOT NULL DEFAULT '',       -- provenance, never scope
    wo_id TEXT NOT NULL DEFAULT '',
    approval_id INTEGER,                    -- the dismissal this was learned from
    reason TEXT NOT NULL DEFAULT '',        -- the reviewer's words, verbatim
    hits INTEGER NOT NULL DEFAULT 0,        -- how often it has cleared a command
    last_hit REAL,
    retired_at REAL,                        -- NULL = in force; set = retracted
    retired_reason TEXT NOT NULL DEFAULT ''
);
-- The self-evolution registry: the gaps the OS has learned to RECOGNISE in itself, and
-- what it proposes doing about each one. See rules.py and
-- docs/superpowers/specs/2026-09-27-self-evolution.md §3.1.
--
-- CENTRAL AND FLEET-WIDE, for the reason `gate_rules` above is: most gaps are OS
-- behaviour rather than one project's, so a rule learned on `jarvis_os` protects every
-- project without being copied into each project's database.
--
-- ONE DIFFERENCE, and it has to be stated because the two tables sit side by side with
-- identically named columns that do NOT mean the same thing: `gate_rules.project` is
-- provenance only, while `detectors.project` is provenance AND an optional SCOPE — `''`
-- means every project, a name means that one. `list_detectors(project=…)` therefore
-- selects `project=? OR project=''`, and the obvious `WHERE project=?` is wrong.
--
-- Two tables and not one: a single row carrying condition and remedy together cannot
-- express "the detector was right and the remedy failed", which is the distinction the
-- recurrence ledger must make, and one detector legitimately accumulates several
-- remedies over time with the older ones retracted.
--
-- Rows here are NEVER deleted and never rewritten in place except the counters, the
-- timestamps and the arm/retract fields — the same append-mostly discipline as
-- `gate_rules` and the knowledge base, because what the OS believed and when is
-- evidence. Retraction writes `retired_at` and a required reason; the row stays.
CREATE TABLE IF NOT EXISTS detectors (
    id TEXT PRIMARY KEY,                  -- 'dt-' + db.new_id
    ts REAL NOT NULL,
    gap_class TEXT NOT NULL,              -- slug, probes.ID_PATTERN shape; the join key
    project TEXT NOT NULL DEFAULT '',     -- provenance AND optional scope; '' = fleet-wide
    subjects TEXT NOT NULL DEFAULT 'work_order',
    condition TEXT NOT NULL,              -- JSON, the rules.py grammar
    summary TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'dry_run',   -- dry_run | armed | retracted
    source TEXT NOT NULL DEFAULT 'builtin',   -- builtin | io | user
    io_id TEXT NOT NULL DEFAULT '', fix_wo_id TEXT NOT NULL DEFAULT '',
    issue_url TEXT NOT NULL DEFAULT '', pr_url TEXT NOT NULL DEFAULT '',
    hits INTEGER NOT NULL DEFAULT 0, last_fired REAL, last_cleared REAL,
    -- The four arming/false-positive columns ship HERE, written by NO code path in this
    -- release: `arm_detector` and `record_false_positive` land with the alarm bridge.
    -- They are here so that later child does not have to buy a migration for a column,
    -- which is the one thing adding a column to a table that already ships costs.
    false_positives INTEGER NOT NULL DEFAULT 0,
    recurrences INTEGER NOT NULL DEFAULT 0,
    arm_threshold INTEGER,                -- recorded, NEVER acted on: only a person arms
    armed_at REAL, armed_by TEXT NOT NULL DEFAULT '',
    armed_reason TEXT NOT NULL DEFAULT '',
    retired_at REAL, retired_reason TEXT NOT NULL DEFAULT '',
    seed_version INTEGER NOT NULL DEFAULT 0
);

-- `status` here uses the SAME three values as `detectors.status` and is never set on its
-- own: A REMEDY ROW FOLLOWS ITS DETECTOR. The alarm bridge's `arm_detector` flips the
-- detector and every non-retracted remedy row under it in ONE transaction; the disarm
-- interlock flips the same set back to `dry_run`; `retract_detector` retracts its remedy
-- rows with the detector's own reason. The ONE independent verb is `retract_remedy_rule`,
-- which retracts one remedy under a LIVE detector — that is how a remedy is replaced
-- without losing the condition's hit history, which is the whole reason the rule is two
-- rows. So a remedy row is never `armed` under a `dry_run` detector, and the EFFECTIVE
-- status of a rule is the WEAKER of the two: read the pair through
-- `rules.effective_status`, never the remedy row alone.
CREATE TABLE IF NOT EXISTS remedy_rules (
    id TEXT PRIMARY KEY,                  -- 'rm-' + db.new_id
    detector_id TEXT NOT NULL REFERENCES detectors(id),
    ts REAL NOT NULL,
    primitive TEXT NOT NULL,              -- a key of remedies.REMEDIES, validated on insert
    params TEXT NOT NULL DEFAULT '{}',
    argument TEXT NOT NULL DEFAULT '',    -- what the gate request says, in words
    status TEXT NOT NULL DEFAULT 'dry_run',
    hits INTEGER NOT NULL DEFAULT 0, last_fired REAL,
    false_positives INTEGER NOT NULL DEFAULT 0,
    retired_at REAL, retired_reason TEXT NOT NULL DEFAULT ''
);

-- `outcome` distinguishes six things that must not be collapsed. `recorded` is a dry
-- run: the condition held and nothing was proposed. `proposed` is an armed fire that
-- raised an alarm; `applied` is one whose remedy the gate then let run, and the two are
-- separate because the headline counts ACTS, not intentions — an alarm nobody approved
-- changed nothing. `refused` is an armed fire the remedy path declined (allow-list,
-- missing grant, precondition) with the refusal's own words in `detail`: not a hit and
-- not a false positive, it is the gate working. `unreadable` is the pinned ruling's
-- case — something could not be READ, nothing was decided, and the row exists so the
-- silence is visible. `cleared` closes a fire.
--
-- The enum ships COMPLETE even though only the evaluation-and-firing section ever writes
-- `proposed`, `applied` or `refused`: a value a later child adds to a column a shipped
-- release already reads is a migration, and there is no reason to buy one.
CREATE TABLE IF NOT EXISTS rule_fires (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
    detector_id TEXT NOT NULL, remedy_rule_id TEXT NOT NULL DEFAULT '',
    project TEXT NOT NULL, order_id TEXT NOT NULL, order_kind TEXT NOT NULL,
    fingerprint TEXT NOT NULL,            -- health.fingerprint, the dedupe memory
    mode TEXT NOT NULL,                   -- dry_run | armed
    outcome TEXT NOT NULL,                -- recorded | proposed | applied | refused
                                          --   | unreadable | cleared
    alarm_id TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    cleared_at REAL, cleared_seconds REAL,
    false_positive INTEGER NOT NULL DEFAULT 0,
    false_positive_reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_rule_fires_detector ON rule_fires(detector_id, ts);
CREATE INDEX IF NOT EXISTS idx_rule_fires_order ON rule_fires(order_id, detector_id);

-- WHEN A GAP THE OS ALREADY HAS A RULE FOR HAPPENS AGAIN (spec §8). One row per
-- recurrence, and THIS LEDGER IS THE OS'S OWN RECORD while the tracker is only a mirror
-- of it: the row is written BEFORE `gh` is touched, so an unreachable tracker, a refused
-- API call or a crash mid-comment cannot lose the finding. What happened on the tracker,
-- or why nothing did, lands afterwards in `filed_note`.
--
-- The verdict set is `rules.RECURRENCE_VERDICTS` and `add_recurrence` validates against
-- it; what each value blames is explained once, in `rules.recurrence_verdict`.
--
-- `filed_note` is the FULL LOCAL ACCOUNT — which act ran, against which issue, or the
-- failure's own words — including anything too private to publish. That is precisely why
-- the tracker comment is built somewhere else, by `rules.recurrence_comment`, from four
-- fields and nothing else: the two texts have different audiences and must not be one
-- string. `note` is the CALLER's own account of this recurrence, stored verbatim.
CREATE TABLE IF NOT EXISTS rule_recurrences (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
    gap_class TEXT NOT NULL,
    detector_id TEXT NOT NULL REFERENCES detectors(id),
    project TEXT NOT NULL, order_id TEXT NOT NULL,
    io_id TEXT NOT NULL DEFAULT '',
    verdict TEXT NOT NULL,               -- missed | remedy_failed | not_armed |
                                         --   unreadable; see rules.recurrence_verdict
    original_fix_wo_id TEXT NOT NULL DEFAULT '',
    original_issue_url TEXT NOT NULL DEFAULT '',
    filed_note TEXT NOT NULL DEFAULT '', -- what happened on the tracker, or why nothing did
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_rule_recurrences_detector
    ON rule_recurrences(detector_id, ts);
-- The append-only history of what the fleet was configured to run, and the only place
-- that record exists: `projects.catalog_json` holds the CURRENT project dict and is
-- overwritten on every `jarvis start`, and the catalog file is untracked, so git is not
-- the history either. See
-- docs/superpowers/specs/2026-08-27-the-config-console.md §2, §9.
--
-- Fleet-wide rather than per project, because project settings resolve AGAINST the `os`
-- block at parse time, several settings have no project to belong to, and traceability
-- wants one id to stamp on a work order. "Per project" is served as a view, not as
-- separate counters.
--
-- TWO JSON documents with two different jobs, and the second is what makes the ledger
-- survive a release. `document_json` is the catalog file's own raw JSON, canonicalised —
-- what the file is rewritten FROM and what the content-addressed id hashes.
-- `resolved_json` is that document parsed and flattened to `path -> value` with every
-- default MATERIALISED at write time, which is what "which config judged this work
-- order" reads: a snapshot storing only the user's sparse keys would silently change
-- meaning on the day a shipped default moved (§2).
--
-- Rows are never rewritten and never migrated. A historical version is evidence of what
-- ran, not configuration anyone needs to run, so it is rendered and diffed — never fed
-- back to `parse_catalog` (§6).
CREATE TABLE IF NOT EXISTS os_config_versions (
    id             TEXT PRIMARY KEY,        -- cfg-<sha256(document_json)[:16]>
    ts             REAL NOT NULL,
    actor          TEXT NOT NULL,           -- user | file | release | <wo-id>
    reason         TEXT NOT NULL DEFAULT '',
    schema_version TEXT NOT NULL,           -- bugreport.jarvis_version() at write time
    document_json  TEXT NOT NULL,           -- canonical catalog document; APPLIED  (§2)
    resolved_json  TEXT NOT NULL,           -- path -> value, defaults frozen; EVIDENCE (§2)
    changes_json   TEXT NOT NULL DEFAULT '[]',  -- the edits the actor asked for
    source_path    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_os_config_versions_ts ON os_config_versions(ts);
CREATE INDEX IF NOT EXISTS idx_gate_rules_role ON gate_rules(role, kind);
CREATE INDEX IF NOT EXISTS idx_inbox_status ON inbox(status);
CREATE INDEX IF NOT EXISTS idx_backlog_project ON backlog(project, status);
CREATE INDEX IF NOT EXISTS idx_agent_calls_wo ON agent_calls(wo_id, ts);
CREATE INDEX IF NOT EXISTS idx_knowledge_reads_wo ON knowledge_reads(wo_id, ts);
CREATE INDEX IF NOT EXISTS idx_knowledge_read_hits ON knowledge_read_hits(kn_id);
"""

# The ranked half of `search_knowledge` — see
# docs/superpowers/specs/2026-08-24-ranked-knowledge-search.md §4. Separate from SCHEMA
# because FTS5 is a compile-time option and a store that will not open is worse than a
# search that is merely as good as yesterday's (§8).
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    content, topic, tags,
    content='knowledge', content_rowid='rowid', tokenize="porter unicode61"
);
CREATE TRIGGER IF NOT EXISTS knowledge_fts_ai AFTER INSERT ON knowledge BEGIN
    INSERT INTO knowledge_fts (rowid, content, topic, tags)
    VALUES (new.rowid, new.content, new.topic, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS knowledge_fts_ad AFTER DELETE ON knowledge BEGIN
    INSERT INTO knowledge_fts (knowledge_fts, rowid, content, topic, tags)
    VALUES ('delete', old.rowid, old.content, old.topic, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS knowledge_fts_au AFTER UPDATE ON knowledge BEGIN
    INSERT INTO knowledge_fts (knowledge_fts, rowid, content, topic, tags)
    VALUES ('delete', old.rowid, old.content, old.topic, old.tags);
    INSERT INTO knowledge_fts (rowid, content, topic, tags)
    VALUES (new.rowid, new.content, new.topic, new.tags);
END;
"""

# One-time backfill marker, NOT a row count — a count on an external-content table
# re-scans `knowledge` and would hide a broken trigger by rebuilding on every open (§4).
FTS_BUILT_KEY = "knowledge_fts_built"

# Which config version the catalog file currently holds — the APPLIED one, which is not
# always the newest row. See `head_config_version`.
CONFIG_HEAD_KEY = "config_head"

# content, topic, tags. A query word in the TOPIC outranks one buried in a long body (§6).
FTS_WEIGHTS = (1.0, 4.0, 2.0)

# Columns added after the first release, exactly as in `neo_store` and `project_store`.
# `CREATE TABLE IF NOT EXISTS` is a NO-OP on a table that already exists, so a column
# added to SCHEMA alone reaches new installs only and every live `os.db` fails on read.
# Until this existed `os.db` had no upgrade path at all — `CentralStore.__init__` ran
# `executescript(SCHEMA)` and nothing else — which is why the first column ever added to
# it had to bring the mechanism with it.
#
# NOTHING HERE FOR `detectors`/`remedy_rules`/`rule_fires`/`rule_recurrences`, and that is
# not an oversight:
# they are NEW tables, so `CREATE TABLE IF NOT EXISTS` creates them in a live `os.db` too.
# This guard is only for a column added to a table that ALREADY ships.
ADDED_COLUMNS = {
    "knowledge": {
        # Retraction. NULL on every pre-existing row, which reads as "standing".
        "retired_at": "REAL",
        "retired_reason": "TEXT",
        # Write attribution (issue #200). '' on every pre-existing row, which reads as
        # "not attributed" — and is exactly what those rows were, since nothing recorded
        # an author until now.
        "wo_id": "TEXT NOT NULL DEFAULT ''",
        "retired_by_wo_id": "TEXT NOT NULL DEFAULT ''",
    },
    "backlog": {
        # Where a deferred item came from. The backlog predates deferral routing, so
        # every pre-existing row reads as "somebody typed this" — NULL origins and an
        # empty note — which is exactly what it was.
        "origin_wo_id": "TEXT",
        "origin_fo_id": "TEXT",
        "origin_note": "TEXT NOT NULL DEFAULT ''",
    },
    "agent_calls": {
        # Which session the call ran in, so its per-API-call detail can be read back
        # from the transcript. '' on every pre-existing row, which reads as "not
        # recorded" — those calls keep their totals and simply cannot be expanded.
        "session_id": "TEXT NOT NULL DEFAULT ''",
        # How big the call's own input was (spec §3,
        # docs/superpowers/specs/2026-09-26-bounded-model-inputs.md). 0 on a
        # pre-existing row means NOT MEASURED, never "an empty prompt".
        "prompt_chars": "INTEGER NOT NULL DEFAULT 0",
        "system_prompt_chars": "INTEGER NOT NULL DEFAULT 0",
        # How long the call took, in milliseconds — §3 of
        # docs/superpowers/specs/2026-10-01-neo-observability.md. NULLABLE, unlike `prompt_chars`
        # beside it: 0 chars of prompt is impossible so 0 can safely mean "not measured"
        # there, whereas a sub-millisecond call rounds to 0 and the report must not print
        # "0 ms" for a call nobody timed.
        "latency_ms": "INTEGER",
    },
}


class CentralStore:
    def __init__(self, path: Path | None = None):
        ensure_home()
        self.db_path = path or central_db_path()
        self.conn = db.connect(self.db_path)
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.fts = self._ensure_fts()
        self._seed_gate_rules()

    def _migrate(self) -> None:
        for table, columns in ADDED_COLUMNS.items():
            have = {
                r["name"]
                for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, decl in columns.items():
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def _ensure_fts(self) -> bool:
        """Create the search index and backfill it once. False = this SQLite has no FTS5.

        docs/superpowers/specs/2026-08-24-ranked-knowledge-search.md §4, §8.
        """
        try:
            self.conn.executescript(FTS_SCHEMA)
            if not self.get_state(FTS_BUILT_KEY):
                self.conn.execute(
                    "INSERT INTO knowledge_fts (knowledge_fts) VALUES ('rebuild')")
                self.set_state(FTS_BUILT_KEY, str(db.now()))
        except sqlite3.Error:
            return False
        return True

    def close(self) -> None:
        self.conn.close()

    # -- projects registry ----------------------------------------------------

    def upsert_project(self, name: str, path: str, description: str = "",
                       model: str | None = None, catalog_json: str = "{}") -> None:
        self.conn.execute(
            """INSERT INTO projects (name, path, description, model, status, last_seen, catalog_json)
               VALUES (?,?,?,?,'active',?,?)
               ON CONFLICT(name) DO UPDATE SET path=excluded.path,
                   description=excluded.description, model=excluded.model,
                   status='active', catalog_json=excluded.catalog_json""",
            (name, str(path), description, model, db.now(), catalog_json),
        )

    def touch_project(self, name: str) -> None:
        self.conn.execute("UPDATE projects SET last_seen=? WHERE name=?", (db.now(), name))

    def set_project_status(self, name: str, status: str) -> None:
        self.conn.execute("UPDATE projects SET status=? WHERE name=?", (status, name))

    def list_projects(self) -> list[dict[str, Any]]:
        return db.rows_to_dicts(
            self.conn.execute("SELECT * FROM projects ORDER BY name").fetchall()
        )

    def get_project(self, name: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

    def project_name_for_path(self, path: str | Path) -> str:
        """The catalog name of the project checked out at `path`.

        A `ProjectStore` knows a path and nothing else, and several callers hold only a
        store — the message bus resolving an envelope's project, the validation panel
        scoping a seat's knowledge base. The registry maps name to path, so the path
        resolves back: a LOOKUP rather than a guess. A project not in the registry falls
        back to its directory name, which is what `jarvis adopt` would have named it.

        ONE IMPLEMENTATION, because two would drift: a panel that scoped its knowledge to
        a name the bus does not use would read a different project's standing
        instructions, and nothing on either side would look wrong.
        """
        try:
            here = Path(path).resolve()
        except OSError:  # pragma: no cover - a path that cannot be resolved is still usable
            here = Path(path)
        for row in self.list_projects():
            try:
                if Path(row["path"]).resolve() == here:
                    return str(row["name"])
            except OSError:  # pragma: no cover
                continue
        return here.name

    # -- inbox ------------------------------------------------------------------

    def add_inbox(self, project: str, title: str, body: str = "", level: str = "info",
                  wo_id: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO inbox (ts, project, level, title, body, wo_id) VALUES (?,?,?,?,?,?)",
            (db.now(), project, level, title, body, wo_id),
        )
        return int(cur.lastrowid)

    def purge_work_order(self, wo_id: str) -> dict[str, int]:
        """Drop every central trace of a deleted work order.

        Inbox items about a work order that no longer exists are noise, and a backlog
        item whose promoted order was deleted goes back to open rather than pointing
        at a ghost.

        The OS's own calls for it go too: `wo delete` is documented as erasing the work
        order and its whole history, and spend attributed to an id nothing can resolve
        would sit in the fleet total for ever with no page able to explain it.
        """
        inbox = self.conn.execute("DELETE FROM inbox WHERE wo_id=?", (wo_id,)).rowcount
        calls = self.conn.execute("DELETE FROM agent_calls WHERE wo_id=?",
                                  (wo_id,)).rowcount
        reads = self.conn.execute("DELETE FROM knowledge_reads WHERE wo_id=?",
                                  (wo_id,)).rowcount
        backlog = self.conn.execute(
            """UPDATE backlog SET status='open', promoted_wo_id=NULL
               WHERE promoted_wo_id=? AND status='promoted'""",
            (wo_id,),
        ).rowcount
        return {"inbox": inbox, "agent_calls": calls, "knowledge_reads": reads,
                "backlog_reopened": backlog}

    def unacked_inbox(self, level: str | None = None) -> list[dict[str, Any]]:
        if level:
            rows = self.conn.execute(
                "SELECT * FROM inbox WHERE status != 'acked' AND level=? ORDER BY ts DESC", (level,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM inbox WHERE status != 'acked' ORDER BY ts DESC"
            ).fetchall()
        return db.rows_to_dicts(rows)

    def new_inbox(self) -> list[dict[str, Any]]:
        return db.rows_to_dicts(
            self.conn.execute("SELECT * FROM inbox WHERE status='new' ORDER BY ts").fetchall()
        )

    def mark_inbox(self, inbox_id: int, status: str, sink_results: Any = None) -> None:
        self.conn.execute(
            "UPDATE inbox SET status=?, sink_results=COALESCE(?, sink_results) WHERE id=?",
            (status, db.to_json(sink_results) if sink_results is not None else None, inbox_id),
        )

    def ack_inbox(self, inbox_id: int | None = None) -> int:
        """Ack one item, or all when inbox_id is None. Returns rows affected."""
        if inbox_id is None:
            cur = self.conn.execute("UPDATE inbox SET status='acked' WHERE status != 'acked'")
        else:
            cur = self.conn.execute("UPDATE inbox SET status='acked' WHERE id=?", (inbox_id,))
        return cur.rowcount

    # -- backlog ------------------------------------------------------------------

    def add_backlog(self, project: str, title: str, description: str = "",
                    depends_on: list[str] | None = None, item_id: str | None = None,
                    origin_wo_id: str | None = None, origin_fo_id: str | None = None,
                    origin_note: str = "") -> dict[str, Any]:
        """File a backlog item. The three `origin_*` arguments are the relationship.

        They default to "nobody deferred this", so every caller that predates deferral
        routing is unaffected — and every caller that DOES have the relationship must
        pass it, whichever side of `bus.deliver` it is on. A row whose origin depends on
        which path filed it is a backlog nobody can query.
        """
        item_id = item_id or db.new_id("bl")
        deps = depends_on or []
        for dep in deps:
            if not self.get_backlog(dep):
                raise KeyError(f"backlog dependency {dep!r} does not exist")
        self.conn.execute(
            "INSERT INTO backlog (id, project, title, description, depends_on, "
            "created_at, origin_wo_id, origin_fo_id, origin_note) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (item_id, project, title, description, db.to_json(deps), db.now(),
             origin_wo_id, origin_fo_id, origin_note),
        )
        return self.get_backlog(item_id)  # type: ignore[return-value]

    def get_backlog(self, item_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM backlog WHERE id=?", (item_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["depends_on"] = db.from_json(d["depends_on"], [])
        return d

    def list_backlog(self, project: str | None = None, status: str | None = "open") -> list[dict[str, Any]]:
        q = "SELECT * FROM backlog"
        conds, params = [], []
        if project:
            conds.append("project=?"); params.append(project)
        if status:
            conds.append("status=?"); params.append(status)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY created_at"
        out = []
        for row in self.conn.execute(q, params).fetchall():
            d = dict(row)
            d["depends_on"] = db.from_json(d["depends_on"], [])
            out.append(d)
        return out

    def unfinished_dependencies(self, item_id: str) -> list[dict[str, Any]]:
        """Dependencies of item that are not yet done (blockers for promotion)."""
        item = self.get_backlog(item_id)
        if not item:
            raise KeyError(f"backlog item {item_id!r} not found")
        blockers = []
        for dep_id in item["depends_on"]:
            dep = self.get_backlog(dep_id)
            if dep is None or dep["status"] != "done":
                blockers.append(dep or {"id": dep_id, "status": "missing", "title": "?"})
        return blockers

    def mark_backlog(self, item_id: str, status: str, promoted_wo_id: str | None = None) -> None:
        assert status in ("open", "promoted", "done", "dropped"), status
        self.conn.execute(
            "UPDATE backlog SET status=?, promoted_wo_id=COALESCE(?, promoted_wo_id) WHERE id=?",
            (status, promoted_wo_id, item_id),
        )

    def search_backlog(self, words: Sequence[str], project: str | None = None,
                       limit: int = 50) -> list[dict[str, Any]]:
        """Backlog items matching `words`, every status — promoted and done included."""
        expr, params = db.score_sql(words, {
            "id": 3, "title": 3, "description": 1, "origin_note": 1,
        })
        q = [f"SELECT *, {expr} AS _score FROM backlog WHERE _score > 0"]
        if project:
            q.append("AND project=?")
            params.append(project)
        q.append("ORDER BY _score DESC, created_at DESC LIMIT ?")
        rows = self.conn.execute(" ".join(q), (*params, limit)).fetchall()
        return db.rows_to_dicts(rows)

    # -- knowledge -------------------------------------------------------------------

    def add_knowledge(self, content: str, project: str = "", topic: str = "",
                      tags: str = "", wo_id: str = "") -> dict[str, Any]:
        """Write one entry. `wo_id` attributes it — see the column's comment.

        Passed in rather than read from `$JARVIS_WO_ID` here, which is the same shape
        `record_knowledge_read` uses for the read side: this store is a leaf and the
        environment is the CLI's business.
        """
        kid = db.new_id("kn")
        self.conn.execute(
            "INSERT INTO knowledge (id, project, ts, topic, content, tags, wo_id)"
            " VALUES (?,?,?,?,?,?,?)",
            (kid, project, db.now(), topic, content, tags, wo_id),
        )
        return {"id": kid, "project": project, "topic": topic, "content": content,
                "tags": tags, "wo_id": wo_id}

    def retract_knowledge(self, knowledge_id: str, reason: str,
                          wo_id: str = "") -> dict[str, Any]:
        """Retire a knowledge entry the user has superseded. NOT a delete.

        The row stays in the table and keeps being returned by `search_knowledge` — the
        audit trail — while `relevant_knowledge` stops offering it to workers.

        Retracting an already-retired entry RAISES rather than re-stamping it: the
        original reason and timestamp record when the user changed their mind.

        `wo_id` lands in `retired_by_wo_id`, a SEPARATE column from `wo_id`: retracting
        somebody else's entry is the interesting case, not an edge one (wo-28405ea1
        retracted kn-e30648dc, which it had not written), and overwriting the author
        would erase who was originally wrong.
        """
        if not reason.strip():
            raise ValueError("a retraction needs a reason: what supersedes this entry?")
        row = self.conn.execute(
            "SELECT * FROM knowledge WHERE id=?", (knowledge_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"knowledge entry {knowledge_id!r} not found")
        if row["retired_at"] is not None:
            raise ValueError(
                f"knowledge entry {knowledge_id!r} was already retired: "
                f"{row['retired_reason']!r}")
        self.conn.execute(
            "UPDATE knowledge SET retired_at=?, retired_reason=?, retired_by_wo_id=?"
            " WHERE id=?",
            (db.now(), reason.strip(), wo_id, knowledge_id),
        )
        return dict(self.conn.execute(
            "SELECT * FROM knowledge WHERE id=?", (knowledge_id,)).fetchone())

    def knowledge_by_work_order(self, wo_id: str) -> list[dict[str, Any]]:
        """Every knowledge entry this work order wrote or retired, oldest first.

        The query issue #200 said did not exist. Both columns, because a work order that
        retracts an entry and writes its replacement did TWO things and a reviewer
        judging it needs to see both — that was the live case (wo-28405ea1).
        """
        if not wo_id:
            return []
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM knowledge WHERE wo_id=? OR retired_by_wo_id=?"
            " ORDER BY ts ASC", (wo_id, wo_id))]

    def record_memory_file(self, content: str, project: str = "", topic: str = "",
                           tags: str = MEMORY_TAG) -> bool:
        """Mirror a mirrored-from-a-file memory into the knowledge base.

        A memory file is a living document: the worker rewrites it, so the row is
        replaced rather than appended — otherwise every edit would push older
        learnings out of the recency window with near-duplicates of itself.
        Returns False when nothing changed.

        THE SELECT SKIPS RETIRED ROWS, so the next rewrite of a retracted memory file
        INSERTs a fresh live row instead of writing into the retired one. Replacing a
        retired row in place would turn one retraction into a permanent, silent mute on
        a file the worker keeps updating — every later version written somewhere no
        prompt reads, with no signal to anyone. A retraction is a statement about the
        TEXT that was retired, not about the file (ruled by Neo, question 56). Steady
        state is still at most one live row per (project, topic, tags), plus history.
        """
        row = self.conn.execute(
            "SELECT id, content FROM knowledge WHERE project=? AND topic=? AND tags=?"
            " AND retired_at IS NULL ORDER BY ts DESC LIMIT 1",
            (project, topic, tags),
        ).fetchone()
        if row is not None and row["content"] == content:
            return False
        if row is not None:
            self.conn.execute("UPDATE knowledge SET content=?, ts=? WHERE id=?",
                              (content, db.now(), row["id"]))
            return True
        self.add_knowledge(content, project=project, topic=topic, tags=tags)
        return True

    def relevant_knowledge(self, project: str, limit: int = 8,
                           include_retired: bool = False) -> list[dict[str, Any]]:
        """Project-specific + global entries, most recent first.

        This is the PROMPT feed — `dispatch.build_worker_prompt` offers it to every
        worker — so retired entries are excluded by default. `jarvis learn list` passes
        `include_retired=True` and marks them; `search_knowledge` is the unfiltered
        audit surface.
        """
        retired = "" if include_retired else " AND retired_at IS NULL"
        rows = self.conn.execute(
            f"SELECT * FROM knowledge WHERE (project=? OR project='')"
            f"{retired} ORDER BY ts DESC LIMIT ?",
            (project, limit),
        ).fetchall()
        return db.rows_to_dicts(rows)

    def get_knowledge(self, kid: str) -> dict[str, Any] | None:
        """One entry by id — what an index headline cashes in to. Retired entries are
        returned carrying their retirement metadata; the caller marks them."""
        row = self.conn.execute("SELECT * FROM knowledge WHERE id=?", (kid,)).fetchone()
        return dict(row) if row else None

    def search_knowledge(self, term: str, limit: int = 50, project: str | None = None,
                         topic: str | None = None) -> list[dict[str, Any]]:
        """Free-text search. The AUDIT surface: retired entries are included, carrying
        their `retired_at` and `retired_reason`.

        Also the worker's on-demand retrieval verb, which is why retired rows stay in:
        a worker that looked something up and got nothing back would conclude the OS
        knows nothing about it, when the truth is that it knew and changed its mind.
        The row says which, and `cli.cmd_learn` marks it. `project` scopes to that
        project + global; omit it to search the whole fleet (cross-project learnings
        are often the point).

        **TWO TIERS, and the second is a floor** — see
        docs/superpowers/specs/2026-08-24-ranked-knowledge-search.md §3. First the FTS5
        hits ordered by BM25, which is what buys stemming ("rounding" now finds
        "rounded") and rarity-weighted ranking; then the substring hits FTS5 did not
        return, ordered as they always were — how many of the query's words the row
        matched, then recency. Deduplicated by id, then truncated to `limit`. Nothing
        yesterday's search returned is dropped: §2 measures why pure FTS5 would drop
        some ("deploy" stops finding "deployment", because porter stems the two into
        different buckets). Tier 1 decides what comes FIRST, not what comes back.

        Synonyms remain unsolved and are out of reach for any lexical index: this still
        will not find an entry that only ever says "shipit" (§7, on the backlog).
        """
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for tier in (self._search_fts(term, limit, project, topic),
                     self._search_like(term, limit, project, topic)):
            for row in tier:
                if row["id"] not in seen:
                    seen.add(row["id"])
                    rows.append(row)
        return rows[:limit]

    def _search_fts(self, term: str, limit: int, project: str | None,
                    topic: str | None) -> list[dict[str, Any]]:
        """Tier 1: stemmed, BM25-ranked. Empty on a store without FTS5 (§8)."""
        match = fts_query(term)
        if not self.fts or not match:
            return []
        params: list[Any] = [*FTS_WEIGHTS, match]
        q = ["SELECT k.*, bm25(knowledge_fts, ?, ?, ?) AS _rank FROM knowledge k",
             "JOIN knowledge_fts f ON f.rowid = k.rowid",
             "WHERE knowledge_fts MATCH ?"]
        if project is not None:
            q.append("AND (k.project=? OR k.project='')")
            params.append(project)
        if topic is not None:
            q.append("AND k.topic=?")
            params.append(topic)
        q.append("ORDER BY _rank LIMIT ?")  # bm25 is negative: best match is lowest
        params.append(limit)
        try:
            rows = db.rows_to_dicts(self.conn.execute(" ".join(q), params).fetchall())
        except sqlite3.Error:  # a read verb never raises on what a user typed (§5)
            return []
        for row in rows:  # ranking orders the list; it is not a field of an entry
            row.pop("_rank", None)
        return rows

    def _search_like(self, term: str, limit: int, project: str | None,
                     topic: str | None) -> list[dict[str, Any]]:
        """Tier 2: substring, one point per query word the row matched anywhere.

        Catches what stemming splits apart, and keeps the empty term meaning
        "everything" — the read `jarvis learn list` and the dashboard rely on.
        """
        words = db.cap_words([w for w in (term or "").split() if w]) or [""]
        score = " + ".join(
            "(CASE WHEN content LIKE ? OR topic LIKE ? OR tags LIKE ? THEN 1 ELSE 0 END)"
            for _ in words)
        params: list[Any] = []
        for w in words:
            params += [f"%{w}%"] * 3
        q = [f"SELECT *, ({score}) AS _score FROM knowledge WHERE _score > 0"]
        if project is not None:
            q.append("AND (project=? OR project='')")
            params.append(project)
        if topic is not None:
            q.append("AND topic=?")
            params.append(topic)
        q.append("ORDER BY _score DESC, ts DESC LIMIT ?")
        params.append(limit)
        rows = db.rows_to_dicts(self.conn.execute(" ".join(q), params).fetchall())
        for row in rows:
            row.pop("_score", None)
        return rows

    def set_knowledge_tags(self, kid: str, tags: str) -> dict[str, Any] | None:
        self.conn.execute("UPDATE knowledge SET tags=? WHERE id=?", (tags, kid))
        return self.get_knowledge(kid)

    def pin_knowledge(self, kid: str, pinned: bool = True) -> dict[str, Any] | None:
        """Add/remove the `pinned` tag — the switch between 'injected in full into every
        worker prompt' and 'a headline in the index'."""
        row = self.get_knowledge(kid)
        if row is None:
            return None
        tags = split_tags(row["tags"])
        if pinned and PINNED_TAG not in tags:
            tags.append(PINNED_TAG)
        elif not pinned and PINNED_TAG in tags:
            tags.remove(PINNED_TAG)
        return self.set_knowledge_tags(kid, ",".join(tags))

    def count_knowledge(self, project: str, include_retired: bool = False) -> int:
        retired = "" if include_retired else " AND retired_at IS NULL"
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM knowledge WHERE (project=? OR project=''){retired}",
            (project,),
        ).fetchone()
        return int(row["n"])

    def knowledge_topics(self, project: str | None = None,
                         include_retired: bool = False) -> list[tuple[str, int]]:
        """(topic, count) for a project + global entries, biggest topic first.

        Retired entries are excluded by default: this feeds both the prompt's overflow
        roll-call and `jarvis learn topics`, and a topic whose only entries were
        retracted should not advertise itself as somewhere to go looking.
        """
        conds, params = [], []
        if project is not None:
            conds.append("(project=? OR project='')")
            params.append(project)
        if not include_retired:
            conds.append("retired_at IS NULL")
        q = "SELECT topic, COUNT(*) AS n FROM knowledge"
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " GROUP BY topic ORDER BY n DESC, topic"
        return [(r["topic"], int(r["n"]))
                for r in self.conn.execute(q, params).fetchall()]

    def knowledge_brief(self, project: str, pinned_limit: int = 8,
                        digest_limit: int = 40,
                        digest_chars: int = 4000, title: str = "",
                        hint_limit: int = 3,
                        hint_chars: int = 400) -> KnowledgeBrief:
        """Build the bounded prompt view of the knowledge base.

        Cost is capped by `pinned_limit` + `digest_chars` no matter how large the base
        grows; what does not fit degrades to a topic roll-call rather than disappearing.

        This is a PROMPT feed, so it inherits `relevant_knowledge`'s rule: retired
        entries never appear. An index headline is still the prompt — retracting a
        ruling has to remove it from the map as well as from the payload, or the worker
        reads the superseded headline and goes looking for the entry behind it.

        `title` is the only relevance input the selection has — everything else is recency
        and topic round-robin, which is exactly why a term-matched entry tends to land in
        `overflow`. With `title == ""` this behaves as it always did, down to the byte:
        `validation._round`'s shared prefix and `ops._index_cost` both call it positionally
        with the project only and must keep measuring the same prompt.
        """
        brief = KnowledgeBrief(project=project, total=self.count_knowledge(project))
        if brief.total == 0:
            return brief

        rows = db.rows_to_dicts(self.conn.execute(
            "SELECT * FROM knowledge WHERE (project=? OR project='')"
            " AND retired_at IS NULL ORDER BY ts DESC",
            (project,),
        ).fetchall())

        rest: list[dict[str, Any]] = []
        for row in rows:
            if has_tag(row["tags"], PINNED_TAG) and len(brief.pinned) < pinned_limit:
                brief.pinned.append(row)
            else:
                rest.append(row)

        # Selection is round-robin across topics, not straight recency. The index is a
        # map of what the OS knows; letting the one busiest topic consume the whole
        # budget would hide the existence of every other topic — and "I didn't know
        # there was anything to look up" is the exact failure this replaces.
        by_topic: dict[str, list[dict[str, Any]]] = {}
        for row in rest:  # rest is already recency-ordered, so each bucket is too
            by_topic.setdefault(row["topic"], []).append(row)

        selected: list[dict[str, Any]] = []
        spent = 0
        full = False
        while not full and any(by_topic.values()):
            for bucket in by_topic.values():
                if not bucket:
                    continue
                line = headline(bucket[0]["content"])
                if len(selected) >= digest_limit or spent + len(line) > digest_chars:
                    full = True
                    break
                spent += len(line)
                selected.append({**bucket.pop(0), "headline": line})

        # Render grouped: recency picks *what* is shown, topic decides *where* it sits.
        order = {t: i for i, t in enumerate(by_topic)}
        brief.digest = sorted(selected, key=lambda r: (order[r["topic"]], -r["ts"]))
        brief.overflow = sorted(
            ((t, len(rows)) for t, rows in by_topic.items() if rows),
            key=lambda kv: (-kv[1], kv[0]),
        )
        brief.hints = self._title_hints(project, title, hint_limit, hint_chars,
                                        already={r["id"] for r in brief.pinned}
                                        | {r["id"] for r in brief.digest})
        return brief

    def _title_hints(self, project: str, title: str, hint_limit: int, hint_chars: int,
                     already: set[str]) -> list[dict[str, Any]]:
        """Entries whose text matches the work order's own title: headline + id, bounded.

        No `ts` filter, unlike `ops.knowledge_usage_report`'s `could_have_read`. That one
        excludes entries newer than the order so an order is not blamed for failing to read
        what it wrote itself; at dispatch there is no "itself" yet, and filtering would drop
        the newest lessons — the ones a fresh order most needs.
        """
        if not title or hint_limit <= 0 or len(title.split()) < MISSED_MIN_WORDS:
            return []
        hints: list[dict[str, Any]] = []
        spent = 0
        for row in self.search_knowledge(title, limit=hint_limit, project=project):
            # An entry already in the index must not be printed twice, and a retracted one
            # is not a hint — the prompt feed never carries retired entries.
            if row.get("retired_at") or row["id"] in already:
                continue
            line = headline(row["content"])
            if len(hints) >= hint_limit or spent + len(line) > hint_chars:
                break
            spent += len(line)
            hints.append({**row, "headline": line})
        return hints

    # -- who reads the knowledge base --------------------------------------------------

    def record_knowledge_read(self, verb: str, rows: Sequence[dict[str, Any]] = (), *,
                              term: str = "", project: str = "", wo_id: str = "",
                              chars: int | None = None) -> int | None:
        """Write down one retrieval. NEVER RAISES — see `agent_usage`'s closing note.

        An observer that can fail the thing it observes is worse than no observer: a
        worker must not lose a `jarvis learn show` because the OS could not write down
        that it happened. A missing row is visible in the count; a crashed read is not.

        `chars` is what the reader was actually handed, and the caller overrides it when
        that is not the entries' full text: `jarvis learn list` returns headlines unless
        asked for `--full`, and charging it for bodies it never printed would inflate the
        one figure that answers "how much context does memory cost".
        """
        try:
            cur = self.conn.execute(
                "INSERT INTO knowledge_reads (ts, project, wo_id, verb, term, hits, chars)"
                " VALUES (?,?,?,?,?,?,?)",
                (db.now(), project, wo_id, verb, term, len(rows),
                 sum(len(r.get("content") or "") for r in rows)
                 if chars is None else chars),
            )
            read_id = int(cur.lastrowid)
            if verb in AIMED_VERBS:
                self.conn.executemany(
                    "INSERT INTO knowledge_read_hits (read_id, kn_id) VALUES (?,?)",
                    [(read_id, r["id"]) for r in rows if r.get("id")],
                )
            return read_id
        except Exception:  # noqa: BLE001 — accounting never breaks the read it counts
            return None

    def knowledge_reads(self, project: str | None = None, since: float | None = None,
                        limit: int = 2000) -> list[dict[str, Any]]:
        """The raw log, newest first. `project` scopes to reads made FROM that project,
        which is not the same as reads that returned that project's entries: a global
        entry is read from everywhere."""
        conds, params = [], []
        if project:
            conds.append("project=?")
            params.append(project)
        if since is not None:
            conds.append("ts>=?")
            params.append(since)
        where = f" WHERE {' AND '.join(conds)}" if conds else ""
        params.append(limit)
        return db.rows_to_dicts(self.conn.execute(
            f"SELECT * FROM knowledge_reads{where} ORDER BY ts DESC LIMIT ?",
            params).fetchall())

    def knowledge_hit_counts(self, since: float | None = None) -> dict[str, int]:
        """How many aimed reads each entry has answered, by id. Entries never read are
        ABSENT rather than zero — the caller knows the base and can subtract, and a row
        per never-read entry would make the common case the expensive one."""
        q = ("SELECT h.kn_id AS kn_id, COUNT(*) AS n FROM knowledge_read_hits h"
             " JOIN knowledge_reads r ON r.id = h.read_id")
        params: list[Any] = []
        if since is not None:
            q += " WHERE r.ts>=?"
            params.append(since)
        q += " GROUP BY h.kn_id"
        return {r["kn_id"]: int(r["n"]) for r in self.conn.execute(q, params).fetchall()}

    def knowledge_read_summary(self, project: str | None = None,
                               since: float | None = None) -> dict[str, Any]:
        """Totals over the read log: by verb, by who asked, and what came back.

        `misses` counts reads that returned NOTHING. Those are the most informative rows
        in the table and the easiest to lose in an average: an agent asked the base a
        question and the base had no answer, which is a gap in what is recorded, not in
        who reads it.
        """
        conds, params = [], []
        if project:
            conds.append("project=?")
            params.append(project)
        if since is not None:
            conds.append("ts>=?")
            params.append(since)
        where = f" WHERE {' AND '.join(conds)}" if conds else ""
        row = self.conn.execute(
            f"SELECT COUNT(*) AS reads, COALESCE(SUM(chars),0) AS chars,"
            f" COALESCE(SUM(hits),0) AS hits,"
            f" COUNT(DISTINCT CASE WHEN wo_id != '' THEN wo_id END) AS orders,"
            f" SUM(CASE WHEN hits=0 THEN 1 ELSE 0 END) AS misses,"
            f" SUM(CASE WHEN wo_id != '' THEN 1 ELSE 0 END) AS by_workers"
            f" FROM knowledge_reads{where}", params).fetchone()
        out: dict[str, Any] = {k: int(row[k] or 0) for k in
                               ("reads", "chars", "hits", "orders", "misses", "by_workers")}
        out["by_verb"] = {r["verb"]: int(r["n"]) for r in self.conn.execute(
            f"SELECT verb, COUNT(*) AS n FROM knowledge_reads{where}"
            f" GROUP BY verb ORDER BY n DESC", params).fetchall()}
        blank = " AND ".join([*conds, "hits=0", "term != ''"])
        out["unanswered"] = [
            {"verb": r["verb"], "term": r["term"], "wo_id": r["wo_id"], "ts": r["ts"]}
            for r in self.conn.execute(
                f"SELECT verb, term, wo_id, ts FROM knowledge_reads WHERE {blank}"
                f" ORDER BY ts DESC LIMIT 20", params).fetchall()]
        return out

    def knowledge_log_starts(self) -> float | None:
        """When the read log begins, or None if nothing has been recorded yet.

        The boundary every "nobody read this" claim rests on. Work predating it was not
        observed, and counting an unobserved order as one that ignored the knowledge base
        would turn the absence of a measurement into an accusation — the exact failure
        `cost_report` avoids by reporting `found: false` rather than zero.
        """
        row = self.conn.execute("SELECT MIN(ts) AS t FROM knowledge_reads").fetchone()
        return row["t"] if row and row["t"] is not None else None

    def knowledge_reads_by_order(self, since: float | None = None) -> dict[str, int]:
        """How many reads each work order made. The denominator for "did this order
        consult the base at all" lives in the project stores, not here."""
        q = "SELECT wo_id, COUNT(*) AS n FROM knowledge_reads WHERE wo_id != ''"
        params: list[Any] = []
        if since is not None:
            q += " AND ts>=?"
            params.append(since)
        q += " GROUP BY wo_id"
        return {r["wo_id"]: int(r["n"]) for r in self.conn.execute(q, params).fetchall()}

    def knowledge_body_chars(self, project: str | None = None) -> int:
        """Total content characters standing in the base — what the entries WOULD cost
        if they were pasted into a prompt, which is exactly what the index avoids."""
        q = "SELECT COALESCE(SUM(LENGTH(content)),0) AS n FROM knowledge WHERE retired_at IS NULL"
        params: list[Any] = []
        if project:
            q += " AND (project=? OR project='')"
            params.append(project)
        return int(self.conn.execute(q, params).fetchone()["n"])

    # -- the OS's own Claude spend -----------------------------------------------------

    # -- gate rules (what counts as a privileged action; see gate_rules.py) ----

    def _seed_gate_rules(self) -> None:
        """Write the builtin recognisers and canaries, once.

        Guarded by a version key for speed — this runs on every open — but correctness
        rests on the ids, not the guard. They are derived from the rule's content, so an
        insert that has already happened is ignored rather than replayed, and a builtin
        rule the user retracted stays retracted across upgrades and restarts. A random
        id here would silently restore a retired recogniser on every release.
        """
        from . import gate_rules

        if self.get_state("gate_rules_seed") == gate_rules.SEED_VERSION:
            return
        now = db.now()
        for row in gate_rules.seed_rows():
            self.conn.execute(
                """INSERT OR IGNORE INTO gate_rules
                   (id, ts, role, kind, test, pattern, summary, source)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (row["id"], now, row["role"], row["kind"], row["test"], row["pattern"],
                 row.get("summary", ""), row.get("source", "builtin")),
            )
        self.set_state("gate_rules_seed", gate_rules.SEED_VERSION)

    def add_gate_rule(self, role: str, test: str, pattern: str, *, kind: str = "",
                      summary: str = "", source: str = "user", project: str = "",
                      wo_id: str = "", approval_id: int | None = None,
                      reason: str = "", rule_id: str | None = None) -> dict[str, Any]:
        rid = rule_id or db.new_id("gr")
        self.conn.execute(
            """INSERT INTO gate_rules
               (id, ts, role, kind, test, pattern, summary, source, project, wo_id,
                approval_id, reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rid, db.now(), role, kind, test, pattern, summary, source, project, wo_id,
             approval_id, reason),
        )
        return self.get_gate_rule(rid)  # type: ignore[return-value]

    def get_gate_rule(self, rule_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM gate_rules WHERE id=?",
                                (rule_id,)).fetchone()
        return dict(row) if row else None

    def gate_rules(self, role: str | None = None, kind: str | None = None,
                   include_retired: bool = False) -> list[dict[str, Any]]:
        q = "SELECT * FROM gate_rules WHERE 1=1"
        params: list[Any] = []
        if not include_retired:
            q += " AND retired_at IS NULL"
        if role:
            q += " AND role=?"
            params.append(role)
        if kind:
            q += " AND kind=?"
            params.append(kind)
        q += " ORDER BY role, kind, ts"
        return db.rows_to_dicts(self.conn.execute(q, params).fetchall())

    def retract_gate_rule(self, rule_id: str, reason: str) -> dict[str, Any]:
        """Retire a rule without erasing that it was once in force.

        The same shape as `retract_knowledge`, for the same reason: both ledgers are
        append-only, and a rule that turned out to be wrong has to stop applying without
        the record losing what the OS believed and acted on while it did.
        """
        rule = self.get_gate_rule(rule_id)
        if rule is None:
            raise KeyError(f"gate rule {rule_id} not found")
        if rule["retired_at"] is not None:
            raise ValueError(f"gate rule {rule_id} is already retracted")
        self.conn.execute(
            "UPDATE gate_rules SET retired_at=?, retired_reason=? WHERE id=?",
            (db.now(), reason, rule_id),
        )
        return self.get_gate_rule(rule_id)  # type: ignore[return-value]

    # -- the self-evolution registry (detectors, remedy rules, fires; see rules.py) ----
    #
    # docs/superpowers/specs/2026-09-27-self-evolution.md §3. These mirror the
    # `gate_rules` methods above deliberately, retraction semantics included: a retraction
    # NEVER deletes, a reason is required, and a second one raises. Rows are never
    # rewritten in place except the counters, the timestamps and the retract fields —
    # the same append-mostly discipline as `gate_rules` and the knowledge base, because
    # what the OS believed, and when, is evidence.
    #
    # Nothing here seeds: `rules.seed_rows()` is invoked by the section that owns the
    # evaluation pass, and calling it from this module would put builtin rows into every
    # `os.db` before anything could fire them.

    def add_detector(self, gap_class: str, condition: Any, *, project: str = "",
                     subjects: str = "work_order", summary: str = "",
                     source: str = "builtin", io_id: str = "", fix_wo_id: str = "",
                     issue_url: str = "", pr_url: str = "",
                     arm_threshold: int | None = None, seed_version: int = 0,
                     detector_id: str | None = None) -> dict[str, Any]:
        """Register a gap the OS should recognise. ALWAYS in `dry_run`.

        There is no argument that writes an ARMED detector, and there will not be one:
        only a person arms a rule, once its hit history says it is right (spec §3.3).
        Arming lands with the alarm bridge, as `arm_detector`.

        `condition` is JSON text or an already-decoded object, and either way it goes
        through `rules.parse_condition` and is stored CANONICALLY, so nothing unvalidated
        reaches the table. A `RulesError` propagates with every problem in it — the
        caller is a person or a model, and both re-submit per error message.

        `detector_id` is a parameter for the reason `add_gate_rule`'s is: the seeder
        passes a CONTENT-DERIVED id (`rules.seed_id`) so re-seeding is idempotent and
        cannot resurrect a rule the user retracted.

        An `io` detector may not be fleet-wide. Widening one afterwards is a person
        RETRACTING the scoped row and registering a fleet-wide one — which leaves both on
        the record with their reasons — and never an `UPDATE` nobody reviews.
        """
        from . import probes, rules

        if not probes.ID_PATTERN.match(gap_class or ""):
            raise ValueError(
                f"gap_class {gap_class!r} is not a slug — it is the key later sections "
                f"join on, so it must match {probes.ID_PATTERN.pattern}")
        # A rule learned on one project may not silently police the others. Fleet-wide is
        # the POWERFUL case, so it is the REVIEWED one: only `builtin` (seed rows, which
        # are reviewed code in a diff) and `user` (a person typing) may take it. An
        # investigation's rule is scoped to the project that produced it, always.
        # Widening one afterwards is a person retracting the scoped row and registering a
        # fleet-wide one, leaving both on the record with their reasons — never an
        # `UPDATE` nobody reviews (spec §3).
        if source == "io" and not project:
            raise ValueError(
                "an io-learned detector may not be fleet-wide: pass the project that "
                "produced it. A rule learned on one project may not silently police the "
                "others, and only `builtin` and `user` detectors — reviewed code, or a "
                "person typing — may leave `project` empty. To widen this one later, "
                "retract it and register a fleet-wide rule, so both stay on the record "
                "with their reasons.")
        parsed = rules.parse_condition(condition)
        did = detector_id or db.new_id("dt")
        self.conn.execute(
            """INSERT INTO detectors
               (id, ts, gap_class, project, subjects, condition, summary, status,
                source, io_id, fix_wo_id, issue_url, pr_url, arm_threshold,
                seed_version)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (did, db.now(), gap_class, project, subjects,
             json.dumps(parsed, sort_keys=True, separators=(",", ":")), summary,
             rules.DRY_RUN, source, io_id, fix_wo_id, issue_url, pr_url, arm_threshold,
             seed_version),
        )
        return self.get_detector(did)  # type: ignore[return-value]

    def get_detector(self, detector_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM detectors WHERE id=?",
                                (detector_id,)).fetchone()
        return dict(row) if row else None

    def list_detectors(self, *, project: str = "", gap_class: str = "",
                       status: str = "",
                       include_retired: bool = False) -> list[dict[str, Any]]:
        """The registered detectors, oldest first.

        `project` is a SCOPE filter, not an equality filter: it returns the rows scoped
        to that project AND the fleet-wide ones (`project=''`). That is what the column
        means, and the obvious `WHERE project=?` would hide every builtin rule — all of
        which are fleet-wide — from every project that asked.
        """
        q = "SELECT * FROM detectors WHERE 1=1"
        params: list[Any] = []
        if not include_retired:
            q += " AND retired_at IS NULL"
        if project:
            q += " AND (project=? OR project='')"
            params.append(project)
        if gap_class:
            q += " AND gap_class=?"
            params.append(gap_class)
        if status:
            q += " AND status=?"
            params.append(status)
        q += " ORDER BY ts, id"
        return db.rows_to_dicts(self.conn.execute(q, params).fetchall())

    def detectors_for_gap(self, gap_class: str,
                          project: str = "") -> list[dict[str, Any]]:
        """The live detectors for one gap class, in this project's scope."""
        return self.list_detectors(project=project, gap_class=gap_class)

    def add_remedy_rule(self, detector_id: str, primitive: str, *,
                        params: Any = None, argument: str = "",
                        remedy_id: str | None = None) -> dict[str, Any]:
        """What this detector proposes doing. ALWAYS in `dry_run`.

        The primitive and its parameters are validated at INSERT through
        `rules.validate_params`, and every problem is raised at once. The name check is
        the load-bearing one: **the database grows RULES, never PRIMITIVES**
        (kn-6c252734) — a row names one of the closed `remedies.REMEDIES` and can never
        introduce one.

        A retracted detector is refused because a live remedy row under a dead detector
        is a row `rules.resolve` would still pair if anything looked it up by primitive.

        THE STATE MACHINE: a remedy row follows its detector. Same three values as
        `detectors.status`, and nothing here sets `armed` — the alarm bridge's
        `arm_detector` flips the detector and every non-retracted remedy row under it in
        one transaction, the disarm interlock flips the same set back, and
        `retract_detector` retracts them with the detector's reason. The one independent
        verb is `retract_remedy_rule`, which retracts one remedy under a LIVE detector:
        that is how a remedy is replaced without losing the condition's history, which is
        why the rule is two rows. A remedy row is therefore never `armed` under a
        `dry_run` detector, and a rule's EFFECTIVE status is the weaker of the pair —
        read it through `rules.effective_status`, never off this row alone.
        """
        from . import rules

        detector = self.get_detector(detector_id)
        if detector is None:
            raise KeyError(f"detector {detector_id} not found")
        if detector["retired_at"] is not None:
            raise ValueError(f"detector {detector_id} is retracted — a remedy under a "
                             f"retracted detector would never be reached, and would "
                             f"still resolve if anything looked it up by primitive")
        decoded = params if params is not None else {}
        if isinstance(decoded, str):
            decoded = db.from_json(decoded, {})
        problems = rules.validate_params(primitive, decoded)
        if problems:
            raise ValueError("; ".join(problems))
        rid = remedy_id or db.new_id("rm")
        self.conn.execute(
            """INSERT INTO remedy_rules
               (id, detector_id, ts, primitive, params, argument, status)
               VALUES (?,?,?,?,?,?,?)""",
            (rid, detector_id, db.now(), primitive,
             json.dumps(decoded, sort_keys=True, separators=(",", ":")), argument,
             rules.DRY_RUN),
        )
        return self.get_remedy_rule(rid)  # type: ignore[return-value]

    def get_remedy_rule(self, remedy_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM remedy_rules WHERE id=?",
                                (remedy_id,)).fetchone()
        return dict(row) if row else None

    def remedy_rules_for(self, detector_id: str, *,
                         include_retired: bool = False) -> list[dict[str, Any]]:
        q = "SELECT * FROM remedy_rules WHERE detector_id=?"
        if not include_retired:
            q += " AND retired_at IS NULL"
        q += " ORDER BY ts, id"
        return db.rows_to_dicts(self.conn.execute(q, (detector_id,)).fetchall())

    def retract_detector(self, detector_id: str, reason: str) -> dict[str, Any]:
        """Retire a detector, and every live remedy rule under it.

        The cascade is the point. A live remedy row whose detector is retracted is a row
        `rules.resolve` would still pair the moment anything looked one up by primitive
        rather than by detector, so it is retired in the same breath with a reason saying
        it followed its detector. Nothing is deleted: `list_detectors(include_retired=
        True)` still returns the row, because a rule that turned out to be wrong has to
        stop applying without the record losing what the OS believed while it did.
        """
        from . import rules

        detector = self.get_detector(detector_id)
        if detector is None:
            raise KeyError(f"detector {detector_id} not found")
        if detector["retired_at"] is not None:
            raise ValueError(f"detector {detector_id} is already retracted")
        now = db.now()
        self.conn.execute(
            "UPDATE detectors SET status=?, retired_at=?, retired_reason=? WHERE id=?",
            (rules.RETRACTED, now, reason, detector_id),
        )
        self.conn.execute(
            """UPDATE remedy_rules SET status=?, retired_at=?, retired_reason=?
               WHERE detector_id=? AND retired_at IS NULL""",
            (rules.RETRACTED, now,
             f"followed its detector {detector_id}, retracted: {reason}", detector_id),
        )
        return self.get_detector(detector_id)  # type: ignore[return-value]

    def retract_remedy_rule(self, remedy_id: str, reason: str) -> dict[str, Any]:
        rule = self.get_remedy_rule(remedy_id)
        if rule is None:
            raise KeyError(f"remedy rule {remedy_id} not found")
        if rule["retired_at"] is not None:
            raise ValueError(f"remedy rule {remedy_id} is already retracted")
        from . import rules

        self.conn.execute(
            "UPDATE remedy_rules SET status=?, retired_at=?, retired_reason=? WHERE id=?",
            (rules.RETRACTED, db.now(), reason, remedy_id),
        )
        return self.get_remedy_rule(remedy_id)  # type: ignore[return-value]

    def record_rule_fire(self, *, detector_id: str, project: str, order_id: str,
                         order_kind: str, fingerprint: str, mode: str, outcome: str,
                         remedy_rule_id: str = "", alarm_id: str = "",
                         detail: str = "") -> dict[str, Any]:
        """Record one thing the registry noticed about one order.

        ONLY A HIT COUNTS. `recorded`, `proposed` and `applied` increment `hits` and move
        `last_fired`; `refused`, `unreadable` and `cleared` do not, and the distinction is
        the whole reason the enum has six values. A refusal is the GATE WORKING, not the
        detector being right — counting it would calibrate an arm threshold on evidence
        that says nothing about the rule. An `unreadable` is not a decision at all: it is
        the record that something could not be read and nothing was decided. A `cleared`
        closes a fire that was already counted when it opened.

        `detail` is bounded through `rules.bound`, the way every other payload in this
        codebase is, with the cap recorded in the text.
        """
        from . import rules

        if outcome not in rules.FIRE_OUTCOMES:
            raise ValueError(f"unknown fire outcome {outcome!r} — expected one of "
                             f"{', '.join(rules.FIRE_OUTCOMES)}")
        now = db.now()
        cur = self.conn.execute(
            """INSERT INTO rule_fires
               (ts, detector_id, remedy_rule_id, project, order_id, order_kind,
                fingerprint, mode, outcome, alarm_id, detail)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (now, detector_id, remedy_rule_id, project, order_id, order_kind,
             fingerprint, mode, outcome, alarm_id, rules.bound(detail)),
        )
        if outcome in (rules.RECORDED, rules.PROPOSED, rules.APPLIED):
            self.conn.execute(
                "UPDATE detectors SET hits = hits + 1, last_fired=? WHERE id=?",
                (now, detector_id))
            if remedy_rule_id:
                self.conn.execute(
                    "UPDATE remedy_rules SET hits = hits + 1, last_fired=? WHERE id=?",
                    (now, remedy_rule_id))
        return self.get_rule_fire(int(cur.lastrowid))  # type: ignore[return-value]

    def get_rule_fire(self, fire_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM rule_fires WHERE id=?",
                                (fire_id,)).fetchone()
        return dict(row) if row else None

    def open_rule_fire(self, detector_id: str,
                       order_id: str) -> dict[str, Any] | None:
        """The NEWEST uncleared fire for this detector on this order. The dedupe memory.

        A condition standing for six hours is one fire, not 720, and the dedupe is
        against the NEWEST record rather than against every past one — commit `0c1e3f9`
        ("Dedupe a hold against the newest one, not every past one") already had to learn
        that once, on the holds ledger, where matching any past row meant a condition
        that recurred after being cleared was silently swallowed for ever.
        """
        row = self.conn.execute(
            """SELECT * FROM rule_fires
               WHERE detector_id=? AND order_id=? AND cleared_at IS NULL
               ORDER BY ts DESC, id DESC LIMIT 1""",
            (detector_id, order_id)).fetchone()
        return dict(row) if row else None

    def close_rule_fire(self, fire_id: int, *,
                        now: float | None = None) -> dict[str, Any]:
        """Close a fire: the condition no longer holds, and this detector may fire on
        this order again.

        `cleared_seconds` is measured from the fire's OWN `ts` rather than recomputed by
        a caller, so how long a condition stood is a fact of the row and not of whoever
        happened to close it.
        """
        fire = self.get_rule_fire(fire_id)
        if fire is None:
            raise KeyError(f"rule fire {fire_id} not found")
        if fire["cleared_at"] is not None:
            raise ValueError(f"rule fire {fire_id} is already closed")
        ts = db.now() if now is None else now
        self.conn.execute(
            "UPDATE rule_fires SET cleared_at=?, cleared_seconds=? WHERE id=?",
            (ts, max(0.0, ts - float(fire["ts"])), fire_id))
        self.conn.execute("UPDATE detectors SET last_cleared=? WHERE id=?",
                          (ts, fire["detector_id"]))
        return self.get_rule_fire(fire_id)  # type: ignore[return-value]

    def list_rule_fires(self, *, detector_id: str = "", project: str = "",
                        order_id: str = "", outcome: str = "",
                        limit: int = 50) -> list[dict[str, Any]]:
        """The fire history, NEWEST FIRST — this is the one place read as a timeline."""
        q = "SELECT * FROM rule_fires WHERE 1=1"
        params: list[Any] = []
        for column, value in (("detector_id", detector_id), ("project", project),
                              ("order_id", order_id), ("outcome", outcome)):
            if value:
                q += f" AND {column}=?"
                params.append(value)
        q += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(int(limit))
        return db.rows_to_dicts(self.conn.execute(q, params).fetchall())

    # --- the recurrence ledger -------------------------------------------------------
    # docs/superpowers/specs/2026-09-27-self-evolution.md §8. A gap the OS ALREADY has a
    # detector for happened again, which is a different fact from a new gap: the condition
    # missed it, or its remedy did not hold, or it was never armed. Filing that as a fresh
    # issue loses the one thing that matters — that the OS already tried.

    def add_recurrence(self, *, gap_class: str, detector_id: str, project: str,
                       order_id: str, verdict: str, io_id: str = "",
                       original_fix_wo_id: str = "", original_issue_url: str = "",
                       filed_note: str = "", note: str = "") -> dict[str, Any]:
        """Record that an existing rule did not hold, and which half of it did not.

        `verdict` is checked against the closed set for `record_rule_fire`'s reason: a
        verdict is what a finding is filed AGAINST, so an unrecognised one would send the
        fix at a half of the rule nobody judged. The error names the allowed set because
        the caller re-submits per error message.

        `original_fix_wo_id` and `original_issue_url` are COPIED off the detector at write
        time rather than read back through it later: they are the provenance thread the
        recurrence links into, and a detector retracted or re-pointed afterwards must not
        silently rewrite what this row says the OS tried.

        `detectors.recurrences` increments HERE, in the same call, so the counter cannot
        drift from the rows — the same reason `record_rule_fire` owns `hits`.

        `filed_note` and `note` are bounded through `rules.bound`, as every other stored
        payload in this codebase is, with the cap recorded in the text.
        """
        from . import rules

        if verdict not in rules.RECURRENCE_VERDICTS:
            raise ValueError(f"unknown recurrence verdict {verdict!r} — expected one of "
                             f"{', '.join(rules.RECURRENCE_VERDICTS)}")
        cur = self.conn.execute(
            """INSERT INTO rule_recurrences
               (ts, gap_class, detector_id, project, order_id, io_id, verdict,
                original_fix_wo_id, original_issue_url, filed_note, note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (db.now(), gap_class, detector_id, project, order_id, io_id, verdict,
             original_fix_wo_id, original_issue_url, rules.bound(filed_note),
             rules.bound(note)),
        )
        self.conn.execute(
            "UPDATE detectors SET recurrences = recurrences + 1 WHERE id=?",
            (detector_id,))
        return self.get_recurrence(int(cur.lastrowid))  # type: ignore[return-value]

    def get_recurrence(self, recurrence_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM rule_recurrences WHERE id=?",
                                (recurrence_id,)).fetchone()
        return dict(row) if row else None

    def list_recurrences(self, *, detector_id: str = "", project: str = "",
                         order_id: str = "", verdict: str = "",
                         limit: int = 50) -> list[dict[str, Any]]:
        """The recurrence history, NEWEST FIRST — `list_rule_fires`' order, for its
        reason: this is read as a timeline, and the newest row is what a person deciding
        whether to arm or to re-fix the rule is looking at."""
        q = "SELECT * FROM rule_recurrences WHERE 1=1"
        params: list[Any] = []
        for column, value in (("detector_id", detector_id), ("project", project),
                              ("order_id", order_id), ("verdict", verdict)):
            if value:
                q += f" AND {column}=?"
                params.append(value)
        q += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(int(limit))
        return db.rows_to_dicts(self.conn.execute(q, params).fetchall())

    def set_recurrence_filed_note(self, recurrence_id: int,
                                  filed_note: str) -> dict[str, Any]:
        """Write what happened on the tracker onto a row that already exists. THE ONE
        IN-PLACE WRITE in this ledger, and the ordering is why it has to exist.

        §8's rule is that the ledger is the OS's own record and the tracker a mirror of
        it, so the row is inserted BEFORE anything touches GitHub: a `gh` that cannot be
        reached, a refusal, a timeout or a crash between the two calls then loses the
        note and never the finding. The outcome of that act — which issue was reopened or
        commented on, or the failure in its own words — is therefore only knowable
        afterwards, and this is how it reaches the row.

        Nothing else about a recurrence is ever rewritten; the verdict and the provenance
        are what the OS concluded at that moment and stay as written.
        """
        from . import rules

        if self.get_recurrence(recurrence_id) is None:
            raise KeyError(f"rule recurrence {recurrence_id} not found")
        self.conn.execute("UPDATE rule_recurrences SET filed_note=? WHERE id=?",
                          (rules.bound(filed_note), recurrence_id))
        return self.get_recurrence(recurrence_id)  # type: ignore[return-value]

    # --- the config version ledger -------------------------------------------------
    # docs/superpowers/specs/2026-08-27-the-config-console.md §2, §9.

    def add_config_version(
            self, document: Any, resolved: dict[str, Any], *, actor: str,
            reason: str = "", changes: list[dict[str, Any]] | None = None,
            source_path: str = "",
            schema_version: str | None = None) -> dict[str, Any]:
        """Record a configuration snapshot. Returns the EXISTING row when its id is
        already present, writing nothing.

        That is not an optimisation for duplicate calls — it is the meaning of a
        content-addressed id. An edit that changes nothing is not a change, and
        `jarvis config restore` landing back on the id it restored is the same fact seen
        from the other end. The caller learns which happened from the returned row's
        `ts`/`actor`, not from a flag.

        The id is computed here rather than passed in so no call site can write a row
        whose id does not address its own document.

        `actor="release"` is the one exception, and the reason it is decided here rather
        than by the caller: a release rebase records the SAME document resolved under a
        new build (§6.1), which is the same id and so no row at all. Those rows — and
        only those — are addressed by document AND build.
        """
        from . import bugreport, config_version

        build = (schema_version if schema_version is not None
                 else bugreport.jarvis_version())
        vid = config_version.version_id(document,
                                        build=build if actor == "release" else None)
        existing = self.get_config_version(vid)
        if existing is not None:
            self.set_state(CONFIG_HEAD_KEY, vid)
            return existing
        self.conn.execute(
            """INSERT INTO os_config_versions
               (id, ts, actor, reason, schema_version, document_json, resolved_json,
                changes_json, source_path)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (vid, db.now(), actor, reason, build,
             config_version.canonicalise(document),
             db.to_json(resolved), db.to_json(changes or []), source_path),
        )
        self.set_state(CONFIG_HEAD_KEY, vid)
        return self.get_config_version(vid)  # type: ignore[return-value]

    def get_config_version(self, version_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM os_config_versions WHERE id=?", (version_id,)).fetchone()
        return self._config_version_row(row)

    def config_versions(self, limit: int = 50) -> list[dict[str, Any]]:
        """Newest first. `rowid` breaks a tie on `ts` so the order is total: two writes
        inside the same clock tick must still have a head."""
        rows = self.conn.execute(
            "SELECT * FROM os_config_versions ORDER BY ts DESC, rowid DESC LIMIT ?",
            (limit,)).fetchall()
        return [self._config_version_row(r) for r in rows]  # type: ignore[misc]

    def config_versions_since(self, version_id: str) -> int | None:
        """How many versions were written after this one. None if the id is unknown.

        Ordered by `(ts, rowid)`, the same total order `config_versions` uses, so a
        stamp on the head reads 0 rather than one-or-zero depending on the clock.
        """
        row = self.conn.execute(
            "SELECT ts, rowid FROM os_config_versions WHERE id=?",
            (version_id,)).fetchone()
        if row is None:
            return None
        after = self.conn.execute(
            "SELECT COUNT(*) AS n FROM os_config_versions "
            "WHERE ts > ? OR (ts = ? AND rowid > ?)",
            (row["ts"], row["ts"], row["rowid"])).fetchone()
        return int(after["n"])

    def head_config_version(self) -> dict[str, Any] | None:
        """What the fleet is configured to run. None on a fleet that never wrote one —
        which reads as "before the console existed", never as version 1.

        The APPLIED version, not the newest one, and the two differ exactly when
        `jarvis config restore` lands back on an older id: ids are content-addressed, so
        a restore writes no row, and a head read off `ts` would then name a document
        nobody is running — permanent drift out of the one command the design offers as
        a REMEDY for drift (spec §3). The pointer falls back to the newest row so a
        ledger written before it existed still has a head.
        """
        pinned = self.get_state(CONFIG_HEAD_KEY)
        if pinned:
            row = self.get_config_version(pinned)
            if row is not None:
                return row
        versions = self.config_versions(limit=1)
        return versions[0] if versions else None

    @staticmethod
    def _config_version_row(row: Any) -> dict[str, Any] | None:
        """The raw columns plus the three decoded documents, under names without the
        `_json` suffix. Both are kept: the ledger is append-only and its rendering
        surfaces want the stored bytes, while every caller that USES a version wants the
        objects."""
        if row is None:
            return None
        d = dict(row)
        d["document"] = db.from_json(d["document_json"], {})
        d["resolved"] = db.from_json(d["resolved_json"], {})
        d["changes"] = db.from_json(d["changes_json"], [])
        return d

    def record_gate_rule_hit(self, rule_id: str) -> None:
        """Count an exemption actually clearing a command.

        The counterpart to `dismissed_count()`: that number is what the classifier still
        gets wrong, this one is what it has stopped getting wrong. A learned rule with no
        hits is a rule that generalised nothing.
        """
        self.conn.execute(
            "UPDATE gate_rules SET hits = hits + 1, last_hit=? WHERE id=?",
            (db.now(), rule_id),
        )

    def add_agent_call(self, kind: str, *, project: str = "", wo_id: str = "",
                       label: str = "", model: str = "", question_id: int | None = None,
                       ok: bool = True, session_id: str = "",
                       usage: dict[str, Any] | None = None,
                       prompt_chars: int = 0, system_prompt_chars: int = 0,
                       latency_ms: int | None = None) -> int:
        """Record one Claude call the OS made itself. See the `agent_calls` schema.

        `usage` is a `claude_cli.derive_turn_usage` envelope, or None for a call that
        produced none (it errored, or the CLI reported nothing). A None-usage row is
        still WORTH WRITING: it says a call was made and cost something unknown, which
        is a different fact from no call at all, and `ok=False` is what tells a reader
        which. Token columns stay zero there, so it cannot inflate a total.

        `latency_ms=None` is "nobody timed this call" and stays NULL — §3 of
        docs/superpowers/specs/2026-10-01-neo-observability.md.
        """
        u = usage or {}
        cur = self.conn.execute(
            """INSERT INTO agent_calls (ts, project, wo_id, kind, label, model,
                                        question_id, ok, session_id, cost_usd, input,
                                        cache_write, cache_read, output,
                                        prompt_chars, system_prompt_chars, latency_ms,
                                        usage_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (db.now(), project, wo_id, kind, label, model, question_id, 1 if ok else 0,
             session_id,
             u.get("total_cost_usd"), u.get("input") or 0, u.get("cache_write") or 0,
             u.get("cache_read") or 0, u.get("output") or 0,
             prompt_chars, system_prompt_chars, latency_ms,
             db.to_json(usage) if usage else None),
        )
        return int(cur.lastrowid or 0)

    def agent_calls(self, wo_id: str | None = None, project: str | None = None,
                    limit: int = 500) -> list[dict[str, Any]]:
        """The OS's calls, newest first — for one work order, one project, or all."""
        where, params = [], []
        if wo_id is not None:
            where.append("wo_id=?")
            params.append(wo_id)
        if project is not None:
            where.append("project=?")
            params.append(project)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        return db.rows_to_dicts(self.conn.execute(
            f"SELECT * FROM agent_calls {clause} ORDER BY ts DESC LIMIT ?",
            (*params, limit)).fetchall())

    def wo_call_cost(self, wo_id: str) -> float:
        """What the OS ITSELF has spent on one work order, in dollars. One indexed sum.

        The Jarvis half of `budget.Spend` — every Neo answer, panel seat, supervisor
        review and digest charged to this order. Separate from `agent_call_totals`, which
        groups the whole fleet for the cost report: this is asked on every dispatch and
        every reconcile tick of every budgeted order, and it must be one number.
        """
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS c FROM agent_calls WHERE wo_id=?",
            (wo_id,)).fetchone()
        return float(row["c"] or 0.0)

    def agent_call_totals(self, project: str | None = None, *,
                          since: float | None = None,
                          until: float | None = None) -> list[dict[str, Any]]:
        """Every work order's recorded spend, summed in SQL, grouped by kind/label/model.

        Grouped rather than flat because every consumer needs the grouping: the report
        prices each group at its own model's list rate (a digest on Haiku is not Opus
        waste), and the per-work-order view shows what the spend went ON — five panel
        seats reads very differently from one Neo answer.

        `label` joins the key so the WORKER-SUBPROCESS class can be broken down by what
        ran the calls ("pytest: 40 calls") without a second query and, more importantly,
        without a row limit: this is a sum, and a truncated sum understates exactly the
        expensive work order someone is investigating. Consumers that only want kind and
        model re-aggregate in Python, so the finer key costs them nothing.

        One query for the whole fleet: the alternative is a query per work order, and
        the cost report walks every work order there is. `project` is in the key for
        that reason — a fleet-wide consumer opens one project's store at a time and
        `ProjectStore.get_work_order` RAISES on a foreign id, so the column is how a
        row is placed without a lookup (§5a of
        docs/superpowers/specs/2026-10-07-cost-window-selector.md).

        THE TTL SPLIT COMES OUT OF `usage_json`, not out of a column, and summing it here
        is what stops the report under-pricing its own overhead at the 1.25x floor (spec:
        2026-08-22-the-five-minute-write-everywhere.md). `json_extract` returns NULL both
        for a row with no envelope and for one whose envelope predates the field, and
        COALESCE folds both into the same honest zero: no split known, floor rate.

        `max_input_chars` is the largest TOTAL input one call in the group sent: that
        call's prompt plus that call's system prompt, maximised per row and never two
        separate maxima added, which would report a size no call ever sent. MAX and not
        SUM because the question is which call had the biggest input, and a sum of prompt
        sizes is a meaningless number (spec §3,
        docs/superpowers/specs/2026-09-26-bounded-model-inputs.md).
        """
        where, params = [], []
        if project:
            where.append("project=?")
            params.append(project)
        # ADDITIVE, and `None` must keep the whole-table behaviour exactly: `cost_report`
        # asks "what has the fleet spent, ever" and `fleetcost` asks the same question of
        # one usage week (spec §2 of the fleet-cost-distribution spec). Half-open, like
        # every other window in the OS: `ts >= since` and `ts < until`, so two adjacent
        # windows can neither double-count a call nor lose one.
        if since is not None:
            where.append("ts>=?")
            params.append(since)
        if until is not None:
            where.append("ts<?")
            params.append(until)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        return db.rows_to_dicts(self.conn.execute(
            f"""SELECT wo_id, project, kind, label, model, COUNT(*) AS calls,
                       SUM(cost_usd) AS cost_usd, SUM(input) AS input,
                       SUM(cache_write) AS cache_write, SUM(cache_read) AS cache_read,
                       SUM(output) AS output, SUM(1 - ok) AS failed,
                       MAX(prompt_chars + system_prompt_chars) AS max_input_chars,
                       SUM(COALESCE(json_extract(usage_json, '$.cache_1h'), 0))
                           AS cache_1h,
                       SUM(COALESCE(json_extract(usage_json, '$.cache_5m'), 0))
                           AS cache_5m
                FROM agent_calls {clause}
                GROUP BY wo_id, project, kind, label, model""",
            tuple(params)).fetchall())

    def agent_call_totals_by_day(self, project: str | None = None,
                                 since: float | None = None,
                                 kinds: Sequence[str] | None = None,
                                 ) -> list[dict[str, Any]]:
        """`agent_call_totals`' windowed sibling, keyed on (kind, project, day, model).

        A separate query rather than a widened one — §4 of
        docs/superpowers/specs/2026-10-01-neo-observability.md: that one has no `ts` filter and is
        asked on every cost report, and the key it groups on (`wo_id`) is the one this
        report never wants. The DAY BUCKET IS SQL's, as the sums beside it already are.

        `model` stays in the key because the caller prices each group at its own model's
        list rate, which is what `_priced_group` needs and what a blended rate destroys.
        """
        where, params = [], []
        if project:
            where.append("project=?")
            params.append(project)
        if since is not None:
            where.append("ts >= ?")
            params.append(since)
        if kinds:
            where.append(f"kind IN ({','.join('?' for _ in kinds)})")
            params.extend(kinds)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        return db.rows_to_dicts(self.conn.execute(
            f"""SELECT kind, project, model,
                       strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime') AS day,
                       COUNT(*) AS calls, SUM(cost_usd) AS cost_usd,
                       SUM(input) AS input, SUM(cache_write) AS cache_write,
                       SUM(cache_read) AS cache_read, SUM(output) AS output,
                       SUM(1 - ok) AS failed,
                       SUM(COALESCE(json_extract(usage_json, '$.cache_1h'), 0))
                           AS cache_1h,
                       SUM(COALESCE(json_extract(usage_json, '$.cache_5m'), 0))
                           AS cache_5m
                FROM agent_calls {clause}
                GROUP BY kind, project, day, model
                ORDER BY day""", params).fetchall())

    def agent_call_latencies(self, project: str | None = None,
                             since: float | None = None,
                             kinds: Sequence[str] | None = None,
                             ) -> list[dict[str, Any]]:
        """One row per call in the window: its kind and its `latency_ms`, NULL included.

        Percentiles are computed in Python by the caller — SQLite has none, and the row
        count is bounded by the window (§4 of the spec above). NULL rows travel because
        "how much of this window is blind" is a figure the report prints.
        """
        where, params = [], []
        if project:
            where.append("project=?")
            params.append(project)
        if since is not None:
            where.append("ts >= ?")
            params.append(since)
        if kinds:
            where.append(f"kind IN ({','.join('?' for _ in kinds)})")
            params.extend(kinds)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        return db.rows_to_dicts(self.conn.execute(
            f"SELECT kind, latency_ms FROM agent_calls {clause}", params).fetchall())

    # -- os state ----------------------------------------------------------------------

    def set_state(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO os_state (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def get_state(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM os_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_base_health(self, project: str, fact: dict[str, Any]) -> None:
        """Record whether this project's default branch is red. One JSON row per project.

        Spec docs/superpowers/specs/2026-09-26-a-red-default-branch-raises-itself.md §2:
        the fact is fleet-shaped, so it belongs beside `daemon_pid` in `os_state` rather
        than in a per-project table that cannot hold a fleet-shaped key.
        """
        self.set_state(base_health_key(project), json.dumps(fact))

    def base_health(self, project: str) -> dict[str, Any]:
        """The stored reading, or `{}` for "never read" — which is neither red nor green.

        `{}` on unreadable JSON too: a fact that cannot be parsed is one the OS does not
        hold, and rendering it as green is the one direction that must never happen.
        """
        raw = self.get_state(base_health_key(project))
        if not raw:
            return {}
        try:
            fact = json.loads(raw)
        except ValueError:
            return {}
        return fact if isinstance(fact, dict) else {}
