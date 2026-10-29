"""Central store: $JARVIS_HOME/os.db"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import db
from .paths import central_db_path, ensure_home

#: How old a base-health reading may be before the merge pause stops acting on it (docs/superpowers/specs/2026-09-26-a-red-default-branch-raises-itself.md §2)
BASE_HEALTH_FRESH_SECONDS = 900


def base_health_key(project: str) -> str:
    """The `os_state` key holding one project's default-branch health"""
    return f"base_health:{project}"


def base_red_episodes_key(project: str) -> str:
    """The `os_state` key holding one project's red-base episodes"""
    return f"base_red_episodes:{project}"


def advance_red_episodes(episodes: list[list[float | None]], red: bool, now: float,
                         retention_seconds: float) -> list[list[float | None]]:
    """The red-episode list after one TRANSITION, trimmed to the retention bound (docs/superpowers/specs/2026-10-15-a-merge-held-by-a-red-base-is-not-stuck.md)"""
    out: list[list[float | None]] = [[p[0], p[1]] for p in episodes]
    open_pair = out[-1] if out and out[-1][1] is None else None
    if red and open_pair is None:
        out.append([float(now), None])
    elif not red and open_pair is not None:
        open_pair[1] = float(now)
    cutoff = float(now) - float(retention_seconds)
    return [p for p in out if p[1] is None or float(p[1]) >= cutoff]


def base_red_hold_spans(episodes: Sequence[Any], since: float, now: float,
                        open_end: float) -> list[tuple[float, float]]:
    """Every interval inside `[since, now]` this project's default branch was red"""
    spans: list[tuple[float, float]] = []
    for pair in episodes:
        start, end = pair[0], pair[1]
        begin = max(float(start), since)
        finish = min(open_end if end is None else float(end), now)
        if finish > begin:
            spans.append((begin, finish))
    return sorted(spans)


# Tag marking knowledge mirrored out of a Claude Code memory file rather than typed by a worker via...
MEMORY_TAG = "claude-memory"

# Tag marking knowledge that is injected into every worker prompt in full
PINNED_TAG = "pinned"

# How much of an entry survives into the index line
HEADLINE_CHARS = 160

# Read verbs that AIM at an entry, as against the two that sweep the index
AIMED_VERBS = ("show", "search")

#: How many words a work-order title has to carry for a search built out of it to mean anything
MISSED_MIN_WORDS = 3

#: Backlog statuses that SATISFY a `bl-` precondition (docs/superpowers/specs/2026-10-15-a-precondition-is-a-row-not-prose.md §2.6)
SETTLED_BACKLOG_STATUSES = ("done", "dropped")


def split_tags(tags: str) -> list[str]:
    return [t for t in (s.strip() for s in (tags or "").split(",")) if t]


def has_tag(tags: str, tag: str) -> bool:
    return tag in split_tags(tags)


def fts_query(term: str) -> str:
    """A user's words as an FTS5 OR-query (docs/superpowers/specs/2026-08-24-ranked-knowledge-search.md §5.)"""
    words = db.cap_words([w for w in (term or "").split() if any(c.isalnum() for c in w)])
    return " OR ".join('"' + w.replace('"', '""') + '"' for w in words)


def headline(content: str, limit: int = HEADLINE_CHARS) -> str:
    """One-line gist of an entry: its first line, truncated"""
    text = " ".join((content or "").strip().split("\n", 1)[0].split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


@dataclass
class KnowledgeBrief:
    """What a worker prompt says about the knowledge base"""
    project: str
    total: int = 0
    pinned: list[dict[str, Any]] = field(default_factory=list)
    digest: list[dict[str, Any]] = field(default_factory=list)
    overflow: list[tuple[str, int]] = field(default_factory=list)
    hints: list[dict[str, Any]] = field(default_factory=list)
    pinned_chars: int = 0  # 0 = pinned entries render in full

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
    sink_results TEXT,
    paired_reason TEXT                      -- also in ADDED_COLUMNS
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
    -- The four arming/false-positive columns shipped before their writers:
    -- `arm_detector`, `disarm_detector` and `record_false_positive` (the alarm bridge,
    -- self-evolution §6) now write them.
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
    id             TEXT PRIMARY KEY,        -- cfg-<sha256(document_json[+build][+salt])[:16]>
    ts             REAL NOT NULL,
    actor          TEXT NOT NULL,           -- user | file | release | <wo-id>
    reason         TEXT NOT NULL DEFAULT '',
    schema_version TEXT NOT NULL,           -- bugreport.jarvis_version() at write time
    document_json  TEXT NOT NULL,           -- canonical catalog document; APPLIED  (§2)
    resolved_json  TEXT NOT NULL,           -- path -> value, defaults frozen; EVIDENCE (§2)
    changes_json   TEXT NOT NULL DEFAULT '[]',  -- the edits the actor asked for
    source_path    TEXT NOT NULL DEFAULT ''
);
-- One minute's reading of the account's usage windows, or one GAP ROW saying why there
-- is none. Account-level, so central: no project owns the meter. §2 of
-- docs/superpowers/specs/2026-10-08-usage-meter-samples-and-outside-spend.md.
CREATE TABLE IF NOT EXISTS usage_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,                       -- when the sampler READ the endpoint
    ok INTEGER NOT NULL DEFAULT 1,          -- 0 = gap row; every pct/resets column NULL
    reason TEXT NOT NULL DEFAULT '',        -- the failure in words; NEVER the body
    http_status INTEGER,                    -- NULL on a network error that never answered
    latency_ms INTEGER,
    -- The two windows that have existed under stable names since this endpoint did, and
    -- the only two anything reads. NULL on a gap row — never 0.0, which is a MEASUREMENT
    -- meaning "the window just reset".
    five_hour_pct REAL,
    five_hour_resets_at REAL,               -- epoch, parsed from the ISO string
    seven_day_pct REAL,
    seven_day_resets_at REAL,
    -- EVERY OTHER non-null window object, keyed by its top-level name. The payload
    -- carries ~20 further keys and the set is NOT stable (`seven_day_opus`,
    -- `seven_day_sonnet` and about fourteen codenames), so a column per name would need
    -- a migration for every codename Anthropic invents. `limits` is NOT stored: the same
    -- two numbers in a second shape, read by nothing.
    extra_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_usage_samples_ts ON usage_samples(ts);
CREATE INDEX IF NOT EXISTS idx_usage_samples_ok ON usage_samples(ok, ts);

-- Every decision the steward made about work the OS proposed for itself: what, why, and
-- whether it was applied. docs/superpowers/specs/2026-10-21-a-steward-decides-what-the-os-may-spend-on-itself.md §3.2.
-- `applied` is 0 in shadow mode and for any outcome that is not `dispatch`; `outcome` is
-- what the steward WOULD do either way, so a shadow week reads as the enforced one would.
CREATE TABLE IF NOT EXISTS steward_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    project TEXT NOT NULL DEFAULT '',
    origin_kind TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL,
    claimed_severity TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL,                  -- dispatch | queue | drop | ask
    rule TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL,                     -- shadow | enforce
    applied INTEGER NOT NULL DEFAULT 0,
    generation INTEGER NOT NULL DEFAULT 0,
    parent_wo_id TEXT NOT NULL DEFAULT '',
    wo_id TEXT NOT NULL DEFAULT '',         -- the order it led to, once one exists
    budget_usd REAL,
    -- The mechanical dedupe bucket (`steward.dedupe_key`): origin unit + file, '' for a
    -- candidate that names no code; `duplicate_of` names the decision this one restates.
    dedupe_key TEXT NOT NULL DEFAULT '',
    duplicate_of INTEGER NOT NULL DEFAULT 0,
    -- The whole candidate (`steward.Candidate` plus the wordings folded onto it), so a
    -- `queue` can be re-offered later (#1181); `queue_state` is '' | waiting | reoffered |
    -- expired, `queue_note` says why, `queue_ts` when it last changed.
    candidate_json TEXT NOT NULL DEFAULT '{}',
    queue_state TEXT NOT NULL DEFAULT '',
    queue_note TEXT NOT NULL DEFAULT '',
    queue_ts REAL
);
-- Morpheus's queue: one shipped PR being validated in production (docs/superpowers/specs/2026-10-21-morpheus-validates-releases-in-production.md)
CREATE TABLE IF NOT EXISTS morpheus_items (
    id TEXT PRIMARY KEY,                    -- mv-xxxxxxxx
    project TEXT NOT NULL DEFAULT '',       -- the onboarded project the PR belongs to
    repo TEXT NOT NULL DEFAULT '',          -- owner/name
    pr_number INTEGER NOT NULL,
    pr_url TEXT NOT NULL DEFAULT '',
    pr_title TEXT NOT NULL DEFAULT '',
    wo_id TEXT NOT NULL DEFAULT '',         -- the work order the PR title names, if any
    release_tag TEXT NOT NULL DEFAULT '',
    hints TEXT NOT NULL DEFAULT '',         -- the PR's validation section, verbatim
    script TEXT NOT NULL DEFAULT '',
    script_source TEXT NOT NULL DEFAULT '', -- agent | user
    state TEXT NOT NULL,                    -- queued | monitoring | validated | failed | timedout
    created_ts REAL NOT NULL,
    updated_ts REAL NOT NULL,
    deadline_ts REAL NOT NULL,
    last_check_ts REAL,
    next_check_ts REAL,
    checks INTEGER NOT NULL DEFAULT 0,
    last_exit INTEGER,
    last_output TEXT NOT NULL DEFAULT '',
    resolution TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',        -- neo:<qid> | investigation:<id> | improvement:<io-id> | none:<why>
    action_ts REAL,
    auto_requeues INTEGER NOT NULL DEFAULT 0,  -- requeues Neo's own verdict has spent (capped)
    UNIQUE (repo, pr_number)
);
CREATE INDEX IF NOT EXISTS idx_morpheus_items_state ON morpheus_items(state);
CREATE INDEX IF NOT EXISTS idx_steward_decisions_ts ON steward_decisions(ts);
CREATE INDEX IF NOT EXISTS idx_os_config_versions_ts ON os_config_versions(ts);
CREATE INDEX IF NOT EXISTS idx_gate_rules_role ON gate_rules(role, kind);
CREATE INDEX IF NOT EXISTS idx_inbox_status ON inbox(status);
CREATE INDEX IF NOT EXISTS idx_backlog_project ON backlog(project, status);
CREATE INDEX IF NOT EXISTS idx_agent_calls_wo ON agent_calls(wo_id, ts);
CREATE INDEX IF NOT EXISTS idx_knowledge_reads_wo ON knowledge_reads(wo_id, ts);
CREATE INDEX IF NOT EXISTS idx_knowledge_read_hits ON knowledge_read_hits(kn_id);
"""

# The ranked half of `search_knowledge` (docs/superpowers/specs/2026-08-24-ranked-knowledge-search.md §4.)
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

# One-time backfill marker, NOT a row count
FTS_BUILT_KEY = "knowledge_fts_built"

# Which config version the catalog file currently holds
CONFIG_HEAD_KEY = "config_head"

# content, topic, tags
FTS_WEIGHTS = (1.0, 4.0, 2.0)

# Columns added after the first release, exactly as in `neo_store` and `project_store`
ADDED_COLUMNS = {
    "morpheus_items": {
        # A home that ran this table before items were per project
        "project": "TEXT NOT NULL DEFAULT ''",
        # …and before Neo's verdict could act on an item (issue #1254)
        "auto_requeues": "INTEGER NOT NULL DEFAULT 0",
    },
    "inbox": {
        # The attention reason raised WITH this notification, or NULL for a standalone one (io-d2a5dd69)
        "paired_reason": "TEXT",
    },
    "knowledge": {
        # Retraction
        "retired_at": "REAL",
        "retired_reason": "TEXT",
        # Write attribution (issue #200)
        "wo_id": "TEXT NOT NULL DEFAULT ''",
        "retired_by_wo_id": "TEXT NOT NULL DEFAULT ''",
    },
    "backlog": {
        # Where a deferred item came from
        "origin_wo_id": "TEXT",
        "origin_fo_id": "TEXT",
        "origin_note": "TEXT NOT NULL DEFAULT ''",
    },
    "agent_calls": {
        # Which session the call ran in, so its per-API-call detail can be read back from the transcript
        "session_id": "TEXT NOT NULL DEFAULT ''",
        # How big the call's own input was (spec §3 (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)
        "prompt_chars": "INTEGER NOT NULL DEFAULT 0",
        "system_prompt_chars": "INTEGER NOT NULL DEFAULT 0",
        # How long the call took, in milliseconds (docs/superpowers/specs/2026-10-01-neo-observability.md)
        "latency_ms": "INTEGER",
    },
    "steward_decisions": {
        # Dedupe before queue/dispatch (spec §3.2, `steward.dedupe_key`)
        "dedupe_key": "TEXT NOT NULL DEFAULT ''",
        "duplicate_of": "INTEGER NOT NULL DEFAULT 0",
        # Queued means deferred, not dropped (#1181, `steward.drain`)
        "candidate_json": "TEXT NOT NULL DEFAULT '{}'",
        "queue_state": "TEXT NOT NULL DEFAULT ''",
        "queue_note": "TEXT NOT NULL DEFAULT ''",
        "queue_ts": "REAL",
    },
}

# Indexes over columns ADDED_COLUMNS backfills, which SCHEMA runs too early to see
POST_MIGRATION_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_steward_decisions_dedupe
    ON steward_decisions(dedupe_key, duplicate_of);
CREATE INDEX IF NOT EXISTS idx_steward_decisions_queue ON steward_decisions(queue_state);
"""

#: Tables whose `project` is provenance, not scope: purge clears or retracts, never deletes (docs/superpowers/specs/2026-10-28-offboard-a-project.md §3)
PROVENANCE_TABLES = ("detectors", "gate_rules")


class CentralStore:
    def __init__(self, path: Path | None = None):
        ensure_home()
        self.db_path = path or central_db_path()
        self.conn = db.connect(self.db_path)
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.fts = self._ensure_fts()
        self._seed_gate_rules()
        self._seed_detectors()

    def _migrate(self) -> None:
        db.add_missing_columns(self.conn, ADDED_COLUMNS)
        self.conn.executescript(POST_MIGRATION_SCHEMA)

    def _ensure_fts(self) -> bool:
        """Create the search index and backfill it once (docs/superpowers/specs/2026-08-24-ranked-knowledge-search.md §4)"""
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

    # -- projects registry

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
        """The catalog name of the project checked out at `path`"""
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

    # -- inbox

    def record_steward_decision(self, row: dict[str, Any]) -> int:
        """One steward decision (`steward.gate`)"""
        cols = ("project", "origin_kind", "subject", "severity", "claimed_severity",
                "outcome", "rule", "reason", "mode", "applied", "generation",
                "parent_wo_id", "wo_id", "budget_usd", "dedupe_key", "duplicate_of",
                "candidate_json", "queue_state")
        blank: dict[str, Any] = {"applied": 0, "generation": 0, "budget_usd": None,
                                 "duplicate_of": 0, "candidate_json": "{}"}
        cur = self.conn.execute(
            f"INSERT INTO steward_decisions (ts, {', '.join(cols)}) "
            f"VALUES (?{', ?' * len(cols)})",
            (row.get("ts") or db.now(),
             *(row.get(c, blank.get(c, "")) for c in cols)))
        return int(cur.lastrowid)

    def steward_dedupe_bucket(self, dedupe_key: str,
                              limit: int = 50) -> list[dict[str, Any]]:
        """One dedupe bucket's own decisions, newest first: never a duplicate, never a drop"""
        rows = self.conn.execute(
            "SELECT id, ts, severity, outcome, rule, applied, wo_id, subject, candidate_json, "
            "queue_state FROM steward_decisions "
            "WHERE dedupe_key=? AND duplicate_of=0 AND outcome<>'drop' "
            "ORDER BY id DESC LIMIT ?",
            (dedupe_key, limit))
        return [dict(r) for r in rows.fetchall()]

    #: What `update_steward_decision` may re-decide on a recorded row
    STEWARD_REDECIDE = ("severity", "claimed_severity", "outcome", "rule", "reason",
                        "applied", "budget_usd", "subject", "candidate_json",
                        "queue_state", "queue_note", "queue_ts")

    def update_steward_decision(self, decision_id: int,
                                fields: Mapping[str, Any]) -> None:
        """Re-decide a recorded decision in place: a severer wording of its defect (Q2133)"""
        cols = [c for c in self.STEWARD_REDECIDE if c in fields]
        if not cols:
            return
        self.conn.execute(
            f"UPDATE steward_decisions SET {', '.join(f'{c}=?' for c in cols)} WHERE id=?",
            (*(fields[c] for c in cols), decision_id))

    def link_steward_decision(self, decision_id: int, wo_id: str) -> None:
        """Name the order a decision led to, once the caller has created it"""
        self.conn.execute("UPDATE steward_decisions SET wo_id=? WHERE id=?",
                          (wo_id, decision_id))

    def steward_decision(self, decision_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM steward_decisions WHERE id=?",
                                (decision_id,)).fetchone()
        return dict(row) if row else None

    def steward_queue(self, state: str = "waiting") -> list[dict[str, Any]]:
        """The decisions in one queue state, oldest first (`steward.drain`)"""
        rows = self.conn.execute(
            "SELECT * FROM steward_decisions WHERE queue_state=? ORDER BY ts, id", (state,))
        return [dict(r) for r in rows.fetchall()]

    def steward_queue_rows(self, limit: int = 200) -> list[dict[str, Any]]:
        """Every decision ever queued for a re-offer: waiting first, then the latest change"""
        rows = self.conn.execute(
            "SELECT * FROM steward_decisions WHERE queue_state<>'' "
            "ORDER BY queue_state<>'waiting', COALESCE(queue_ts, ts) DESC, id DESC LIMIT ?",
            (limit,))
        return [dict(r) for r in rows.fetchall()]

    def steward_asks(self, since: float, limit: int = 50) -> list[dict[str, Any]]:
        """The `ask` decisions since `since` that led to no order, newest first"""
        rows = self.conn.execute(
            "SELECT * FROM steward_decisions WHERE outcome='ask' AND wo_id='' AND applied=0 "
            "AND duplicate_of=0 AND ts>=? ORDER BY ts DESC, id DESC LIMIT ?", (since, limit))
        return [dict(r) for r in rows.fetchall()]

    def steward_queue_counts(self) -> dict[str, int]:
        """{queue_state: n} over every decision that was ever queued for a re-offer"""
        rows = self.conn.execute(
            "SELECT queue_state, COUNT(*) AS n FROM steward_decisions WHERE queue_state<>'' "
            "GROUP BY queue_state")
        return {r["queue_state"]: int(r["n"]) for r in rows.fetchall()}

    def steward_decisions(self, since: float = 0.0, *, wo_id: str = "",
                          limit: int = 500) -> list[dict[str, Any]]:
        """Decisions newest first, optionally only those about (or leading to) one order"""
        if wo_id:
            rows = self.conn.execute(
                "SELECT * FROM steward_decisions WHERE (wo_id=? OR parent_wo_id=?) "
                "AND ts>=? ORDER BY id DESC LIMIT ?", (wo_id, wo_id, since, limit))
        else:
            rows = self.conn.execute(
                "SELECT * FROM steward_decisions WHERE ts>=? ORDER BY id DESC LIMIT ?",
                (since, limit))
        return [dict(r) for r in rows.fetchall()]

    def call_cost_since(self, since: float) -> float:
        """Everything the OS's own calls cost since `since`, fleet-wide"""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS c FROM agent_calls WHERE ts>=?",
            (since,)).fetchone()
        return float(row["c"] or 0.0)

    def add_inbox(self, project: str, title: str, body: str = "", level: str = "info",
                  wo_id: str | None = None, paired_reason: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO inbox (ts, project, level, title, body, wo_id, paired_reason) "
            "VALUES (?,?,?,?,?,?,?)",
            (db.now(), project, level, title, body, wo_id, paired_reason),
        )
        return int(cur.lastrowid)

    def get_inbox(self, inbox_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM inbox WHERE id=?", (inbox_id,)).fetchone()
        return dict(row) if row else None

    def ack_paired_inbox(self, wo_id: str) -> int:
        """Mark read every notification raised alongside this work order's attention flag"""
        return self.conn.execute(
            "UPDATE inbox SET status='acked' WHERE wo_id=? AND paired_reason IS NOT NULL "
            "AND status != 'acked'", (wo_id,)).rowcount

    def purge_work_order(self, wo_id: str) -> dict[str, int]:
        """Drop every central trace of a deleted work order"""
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

    def purge_project(self, name: str) -> dict[str, int]:
        """Delete every row scoped to `name`; provenance rows keep guarding the fleet"""
        scope = [t for t in db.tables_with_column(self.conn, "project")
                 if t not in PROVENANCE_TABLES]
        with db.write_transaction(self.conn):
            counts = db.delete_where(self.conn, scope, "project", name)
            counts["gate_rules_provenance_cleared"] = self.conn.execute(
                "UPDATE gate_rules SET project='' WHERE project=?", (name,)).rowcount
            live = [r["id"] for r in self.conn.execute(
                "SELECT id FROM detectors WHERE project=? AND retired_at IS NULL", (name,))]
            for detector_id in live:
                self.retract_detector(detector_id, f"project {name} purged from the OS")
            counts["detectors_retracted"] = len(live)
        return counts

    def inbox_titled_since(self, project: str, title: str, since: float) -> bool:
        """Has this exact item been raised for this project since `since`, acked or not?"""
        return self.conn.execute(
            "SELECT 1 FROM inbox WHERE project=? AND title=? AND ts >= ? LIMIT 1",
            (project, title, since)).fetchone() is not None

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
        """Ack one item, or all when inbox_id is None"""
        if inbox_id is None:
            cur = self.conn.execute("UPDATE inbox SET status='acked' WHERE status != 'acked'")
        else:
            cur = self.conn.execute("UPDATE inbox SET status='acked' WHERE id=?", (inbox_id,))
        return cur.rowcount

    def ack_project_inbox(self, project: str) -> int:
        """Ack every unacked item one project raised"""
        return self.conn.execute(
            "UPDATE inbox SET status='acked' WHERE project=? AND status != 'acked'",
            (project,)).rowcount

    # -- backlog

    def add_backlog(self, project: str, title: str, description: str = "",
                    depends_on: list[str] | None = None, item_id: str | None = None,
                    origin_wo_id: str | None = None, origin_fo_id: str | None = None,
                    origin_note: str = "") -> dict[str, Any]:
        """File a backlog item"""
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
        """Dependencies of item that are not yet done (blockers for promotion)"""
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
        """Backlog items matching `words`, every status — promoted and done included"""
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

    # -- knowledge

    def add_knowledge(self, content: str, project: str = "", topic: str = "",
                      tags: str = "", wo_id: str = "") -> dict[str, Any]:
        """Write one entry"""
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
        """Retire a knowledge entry the user has superseded"""
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
        """Every knowledge entry this work order wrote or retired, oldest first (#200)"""
        if not wo_id:
            return []
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM knowledge WHERE wo_id=? OR retired_by_wo_id=?"
            " ORDER BY ts ASC", (wo_id, wo_id))]

    def record_memory_file(self, content: str, project: str = "", topic: str = "",
                           tags: str = MEMORY_TAG) -> bool:
        """Mirror a mirrored-from-a-file memory into the knowledge base"""
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
        """Project-specific + global entries, most recent first"""
        retired = "" if include_retired else " AND retired_at IS NULL"
        rows = self.conn.execute(
            f"SELECT * FROM knowledge WHERE (project=? OR project='')"
            f"{retired} ORDER BY ts DESC LIMIT ?",
            (project, limit),
        ).fetchall()
        return db.rows_to_dicts(rows)

    def get_knowledge(self, kid: str) -> dict[str, Any] | None:
        """One entry by id — what an index headline cashes in to"""
        row = self.conn.execute("SELECT * FROM knowledge WHERE id=?", (kid,)).fetchone()
        return dict(row) if row else None

    def search_knowledge(self, term: str, limit: int = 50, project: str | None = None,
                         topic: str | None = None) -> list[dict[str, Any]]:
        """Free-text search (docs/superpowers/specs/2026-08-24-ranked-knowledge-search.md §3.)"""
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
        """Tier 1: stemmed, BM25-ranked"""
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
        """Tier 2: substring, one point per query word the row matched anywhere"""
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
        """Add/remove the `pinned` tag — the switch between 'injected in full into every worker prompt' and 'a..."""
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
        """(topic, count) for a project + global entries, biggest topic first"""
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
                        hint_chars: int = 400,
                        pinned_chars: int = 0) -> KnowledgeBrief:
        """Build the bounded prompt view of the knowledge base"""
        brief = KnowledgeBrief(project=project, total=self.count_knowledge(project),
                               pinned_chars=pinned_chars)
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

        # Selection is round-robin across topics, not straight recency
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

        # Render grouped: recency picks *what* is shown, topic decides *where* it sits
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
        """Entries whose text matches the work order's own title: headline + id, bounded"""
        if not title or hint_limit <= 0 or len(title.split()) < MISSED_MIN_WORDS:
            return []
        hints: list[dict[str, Any]] = []
        spent = 0
        for row in self.search_knowledge(title, limit=hint_limit, project=project):
            # An entry already in the index must not be printed twice, and a retracted one is not a hint
            if row.get("retired_at") or row["id"] in already:
                continue
            line = headline(row["content"])
            if len(hints) >= hint_limit or spent + len(line) > hint_chars:
                break
            spent += len(line)
            hints.append({**row, "headline": line})
        return hints

    # -- who reads the knowledge base

    def record_knowledge_read(self, verb: str, rows: Sequence[dict[str, Any]] = (), *,
                              term: str = "", project: str = "", wo_id: str = "",
                              chars: int | None = None) -> int | None:
        """Write down one retrieval"""
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
        """The raw log, newest first"""
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
        """How many aimed reads each entry has answered, by id"""
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
        """Totals over the read log: by verb, by who asked, and what came back"""
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
        """When the read log begins, or None if nothing has been recorded yet"""
        row = self.conn.execute("SELECT MIN(ts) AS t FROM knowledge_reads").fetchone()
        return row["t"] if row and row["t"] is not None else None

    def knowledge_reads_by_order(self, since: float | None = None) -> dict[str, int]:
        """How many reads each work order made"""
        q = "SELECT wo_id, COUNT(*) AS n FROM knowledge_reads WHERE wo_id != ''"
        params: list[Any] = []
        if since is not None:
            q += " AND ts>=?"
            params.append(since)
        q += " GROUP BY wo_id"
        return {r["wo_id"]: int(r["n"]) for r in self.conn.execute(q, params).fetchall()}

    def knowledge_body_chars(self, project: str | None = None) -> int:
        """Total content characters standing in the base"""
        q = "SELECT COALESCE(SUM(LENGTH(content)),0) AS n FROM knowledge WHERE retired_at IS NULL"
        params: list[Any] = []
        if project:
            q += " AND (project=? OR project='')"
            params.append(project)
        return int(self.conn.execute(q, params).fetchone()["n"])

    # -- the OS's own Claude spend

    # -- gate rules (what counts as a privileged action; see gate_rules.py)

    def _seed_gate_rules(self) -> None:
        """Write the builtin recognisers and canaries, once"""
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
        """Retire a rule without erasing that it was once in force"""
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

    # -- the self-evolution registry (detectors, remedy rules, fires; see rules.py)

    def _seed_detectors(self) -> None:
        """Write the five builtin detectors and their remedy rows, once"""
        from . import rules

        # COMPARED AS TEXT, and the `str()` is load-bearing
        if str(self.get_state("detectors_seed") or "") == str(rules.SEED_VERSION):
            return
        now = db.now()
        for seed in rules.seed_rows():
            det = seed["detector"]
            self.conn.execute(
                """INSERT OR IGNORE INTO detectors
                   (id, ts, gap_class, project, subjects, condition, summary, status,
                    source, issue_url, seed_version)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (det["id"], now, det["gap_class"], det["project"], det["subjects"],
                 det["condition"], det["summary"], det["status"], det["source"],
                 det["issue_url"], det["seed_version"]),
            )
            for row in seed["remedies"]:
                self.conn.execute(
                    """INSERT OR IGNORE INTO remedy_rules
                       (id, detector_id, ts, primitive, params, argument, status)
                       VALUES (?,?,?,?,?,?,?)""",
                    (row["id"], row["detector_id"], now, row["primitive"],
                     row["params"], row["argument"], row["status"]),
                )
        self.set_state("detectors_seed", str(rules.SEED_VERSION))

    def add_detector(self, gap_class: str, condition: Any, *, project: str = "",
                     subjects: str = "work_order", summary: str = "",
                     source: str = "builtin", io_id: str = "", fix_wo_id: str = "",
                     issue_url: str = "", pr_url: str = "",
                     arm_threshold: int | None = None, seed_version: int = 0,
                     detector_id: str | None = None) -> dict[str, Any]:
        """Register a gap the OS should recognise"""
        from . import probes, rules

        if not probes.ID_PATTERN.match(gap_class or ""):
            raise ValueError(
                f"gap_class {gap_class!r} is not a slug — it is the key later sections "
                f"join on, so it must match {probes.ID_PATTERN.pattern}")
        # A rule learned on one project may not silently police the others
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
        """The registered detectors, oldest first"""
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
        """The live detectors for one gap class, in this project's scope"""
        return self.list_detectors(project=project, gap_class=gap_class)

    def add_remedy_rule(self, detector_id: str, primitive: str, *,
                        params: Any = None, argument: str = "",
                        remedy_id: str | None = None) -> dict[str, Any]:
        """What this detector proposes doing"""
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
        """Retire a detector, and every live remedy rule under it"""
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

    def arm_detector(self, detector_id: str, *, by: str, reason: str) -> dict[str, Any]:
        """Let a detector's fires act: the detector and EVERY live remedy row, one commit (docs/superpowers/specs/2026-09-27-self-evolution.md)"""
        from . import rules

        detector = self.get_detector(detector_id)
        if detector is None:
            raise KeyError(f"detector {detector_id} not found")
        if detector["retired_at"] is not None or detector["status"] == rules.RETRACTED:
            raise ValueError(f"detector {detector_id} is retracted — it cannot be armed")
        if detector["status"] == rules.ARMED:
            raise ValueError(f"detector {detector_id} is already armed")
        if not (reason or "").strip():
            raise ValueError("arming a rule needs a reason — it is the only record of why "
                             "the OS let this rule act")
        now = db.now()
        with db.write_transaction(self.conn):  # the pair flips together: db.write_transaction
            self.conn.execute(
                "UPDATE detectors SET status=?, armed_at=?, armed_by=?, armed_reason=? "
                "WHERE id=?", (rules.ARMED, now, by, reason.strip(), detector_id))
            self.conn.execute(
                "UPDATE remedy_rules SET status=? WHERE detector_id=? AND retired_at IS NULL",
                (rules.ARMED, detector_id))
        return self.get_detector(detector_id)  # type: ignore[return-value]

    def disarm_detector(self, detector_id: str, reason: str) -> dict[str, Any]:
        """The same set back to `dry_run`"""
        from . import rules

        detector = self.get_detector(detector_id)
        if detector is None:
            raise KeyError(f"detector {detector_id} not found")
        if detector["status"] != rules.ARMED:
            raise ValueError(f"detector {detector_id} is {detector['status']}, not armed")
        if not (reason or "").strip():
            raise ValueError("returning a rule to dry run needs a reason")
        with db.write_transaction(self.conn):  # the pair flips together: db.write_transaction
            self.conn.execute("UPDATE detectors SET status=? WHERE id=?",
                              (rules.DRY_RUN, detector_id))
            self.conn.execute(
                "UPDATE remedy_rules SET status=? WHERE detector_id=? AND retired_at IS NULL",
                (rules.DRY_RUN, detector_id))
        return self.get_detector(detector_id)  # type: ignore[return-value]

    def record_false_positive(self, fire_id: int, reason: str) -> dict[str, Any]:
        """A PERSON says this fire was wrong"""
        from . import rules

        fire = self.get_rule_fire(int(fire_id))
        if fire is None:
            raise KeyError(f"rule fire {fire_id} not found")
        if fire["false_positive"]:
            raise ValueError(f"rule fire {fire_id} is already marked a false positive")
        if not (reason or "").strip():
            raise ValueError("a false positive needs a reason — it is the evidence a "
                             "later arm is judged on")
        if fire["outcome"] in (rules.UNREADABLE, rules.CLEARED):
            raise ValueError(f"rule fire {fire_id} is {fire['outcome']} — the rule decided "
                             f"nothing there, so there is nothing to call wrong")
        with db.write_transaction(self.conn):  # one commit: db.write_transaction
            self.conn.execute(
                "UPDATE rule_fires SET false_positive=1, false_positive_reason=? WHERE id=?",
                (rules.bound(reason), int(fire_id)))
            self.conn.execute(
                "UPDATE detectors SET false_positives = false_positives + 1 WHERE id=?",
                (fire["detector_id"],))
            if fire["remedy_rule_id"]:
                self.conn.execute(
                    "UPDATE remedy_rules SET false_positives = false_positives + 1 "
                    "WHERE id=?", (fire["remedy_rule_id"],))
        return self.get_rule_fire(int(fire_id))  # type: ignore[return-value]

    def record_rule_fire(self, *, detector_id: str, project: str, order_id: str,
                         order_kind: str, fingerprint: str, mode: str, outcome: str,
                         remedy_rule_id: str = "", alarm_id: str = "",
                         detail: str = "") -> dict[str, Any]:
        """Record one thing the registry noticed about one order"""
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
        """The NEWEST uncleared fire for this detector on this order"""
        row = self.conn.execute(
            """SELECT * FROM rule_fires
               WHERE detector_id=? AND order_id=? AND cleared_at IS NULL
               ORDER BY ts DESC, id DESC LIMIT 1""",
            (detector_id, order_id)).fetchone()
        return dict(row) if row else None

    def close_rule_fire(self, fire_id: int, *,
                        now: float | None = None) -> dict[str, Any]:
        """Close a fire: the condition no longer holds, and this detector may fire on this order again"""
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
        """The fire history, NEWEST FIRST — this is the one place read as a timeline"""
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

    # --- the recurrence ledger

    def add_recurrence(self, *, gap_class: str, detector_id: str, project: str,
                       order_id: str, verdict: str, io_id: str = "",
                       original_fix_wo_id: str = "", original_issue_url: str = "",
                       filed_note: str = "", note: str = "") -> dict[str, Any]:
        """Record that an existing rule did not hold, and which half of it did not"""
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
        """The recurrence history, NEWEST FIRST"""
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
        """Write what happened on the tracker onto a row that already exists"""
        from . import rules

        if self.get_recurrence(recurrence_id) is None:
            raise KeyError(f"rule recurrence {recurrence_id} not found")
        self.conn.execute("UPDATE rule_recurrences SET filed_note=? WHERE id=?",
                          (rules.bound(filed_note), recurrence_id))
        return self.get_recurrence(recurrence_id)  # type: ignore[return-value]

    # --- the config version ledger

    def add_config_version(
            self, document: Any, resolved: dict[str, Any], *, actor: str,
            reason: str = "", changes: list[dict[str, Any]] | None = None,
            source_path: str = "",
            schema_version: str | None = None) -> dict[str, Any]:
        """Record a configuration snapshot"""
        from . import bugreport, config_version

        build = (schema_version if schema_version is not None
                 else bugreport.jarvis_version())
        salted = build if actor == "release" else None
        vid = config_version.version_id(document, build=salted)
        head = self.head_config_version()
        # An edit that changes nothing: same document, same build, already head (1235)
        if (head is not None and head["schema_version"] == build
                and config_version.canonicalise(head["document"])
                == config_version.canonicalise(document)):
            return head
        ts = db.now()
        attempt = 0
        while self.get_config_version(vid) is not None:
            # A repeat of an OLDER document is its own event, and head never rewinds (1235)
            attempt += 1
            vid = config_version.version_id(document, build=salted,
                                            salt=f"{ts}#{attempt}")
        self.conn.execute(
            """INSERT INTO os_config_versions
               (id, ts, actor, reason, schema_version, document_json, resolved_json,
                changes_json, source_path)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (vid, ts, actor, reason, build,
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
        """Newest first"""
        rows = self.conn.execute(
            "SELECT * FROM os_config_versions ORDER BY ts DESC, rowid DESC LIMIT ?",
            (limit,)).fetchall()
        return [self._config_version_row(r) for r in rows]  # type: ignore[misc]

    def config_versions_since(self, version_id: str) -> int | None:
        """How many versions were written after this one"""
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
        """What the fleet is configured to run"""
        pinned = self.get_state(CONFIG_HEAD_KEY)
        if pinned:
            row = self.get_config_version(pinned)
            if row is not None:
                return row
        versions = self.config_versions(limit=1)
        return versions[0] if versions else None

    @staticmethod
    def _config_version_row(row: Any) -> dict[str, Any] | None:
        """The raw columns plus the three decoded documents, under names without the `_json` suffix"""
        if row is None:
            return None
        d = dict(row)
        d["document"] = db.from_json(d["document_json"], {})
        d["resolved"] = db.from_json(d["resolved_json"], {})
        d["changes"] = db.from_json(d["changes_json"], [])
        return d

    def record_gate_rule_hit(self, rule_id: str) -> None:
        """Count an exemption actually clearing a command"""
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
        """Record one Claude call the OS made itself (docs/superpowers/specs/2026-10-01-neo-observability.md)"""
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
        """The OS's calls, newest first — for one work order, one project, or all"""
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
        """What the OS ITSELF has spent on one work order, in dollars"""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS c FROM agent_calls WHERE wo_id=?",
            (wo_id,)).fetchone()
        return float(row["c"] or 0.0)

    def agent_call_totals(self, project: str | None = None, *,
                          since: float | None = None,
                          until: float | None = None) -> list[dict[str, Any]]:
        """Every work order's recorded spend, summed in SQL, grouped by kind/label/model (docs/superpowers/specs/2026-10-07-cost-window-selector.md)"""
        where, params = [], []
        if project:
            where.append("project=?")
            params.append(project)
        # ADDITIVE, and `None` must keep the whole-table behaviour exactly: `cost_report` asks "what has the...
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
        """`agent_call_totals`' windowed sibling, keyed on (kind, project, day, model) (docs/superpowers/specs/2026-10-01-neo-observability.md)"""
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
        """One row per call in the window: its kind and its `latency_ms`, NULL included"""
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

    # -- morpheus: production validation of shipped PRs

    def add_morpheus_item(self, *, project: str, repo: str, pr_number: int, pr_url: str, pr_title: str,
                          wo_id: str, release_tag: str, hints: str, script: str,
                          script_source: str, state: str, deadline_ts: float,
                          resolution: str = "", now: float | None = None
                          ) -> dict[str, Any] | None:
        """Queue one PR; None when that PR is already queued (one item per PR, ever)"""
        ts = db.now() if now is None else now
        item_id = db.new_id("mv")
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO morpheus_items
               (id, project, repo, pr_number, pr_url, pr_title, wo_id, release_tag, hints,
                script, script_source, state, created_ts, updated_ts, deadline_ts,
                next_check_ts, resolution)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (item_id, project, repo, pr_number, pr_url, pr_title, wo_id, release_tag, hints,
             script, script_source, state, ts, ts, deadline_ts,
             ts if state == "monitoring" else None, resolution))
        return self.morpheus_item(item_id) if cur.rowcount else None

    def morpheus_item(self, item_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM morpheus_items WHERE id=?",
                                (item_id,)).fetchone()
        return dict(row) if row else None

    def morpheus_items(self, states: Sequence[str] | None = None,
                       limit: int = 500) -> list[dict[str, Any]]:
        """Newest first"""
        where, params = "", []
        if states:
            where = f"WHERE state IN ({','.join('?' * len(states))})"
            params = list(states)
        return db.rows_to_dicts(self.conn.execute(
            f"SELECT * FROM morpheus_items {where} ORDER BY created_ts DESC, pr_number DESC "
            f"LIMIT ?", (*params, limit)).fetchall())

    def morpheus_counts(self) -> dict[str, int]:
        return {r["state"]: r["n"] for r in self.conn.execute(
            "SELECT state, COUNT(*) AS n FROM morpheus_items GROUP BY state").fetchall()}

    def update_morpheus_item(self, item_id: str, **fields: Any) -> None:
        """Write the named columns, stamping `updated_ts`"""
        allowed = {"script", "script_source", "state", "deadline_ts", "last_check_ts",
                   "next_check_ts", "checks", "last_exit", "last_output", "resolution",
                   "action", "action_ts", "hints"}
        bad = set(fields) - allowed
        assert not bad, bad
        fields["updated_ts"] = db.now()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE morpheus_items SET {cols} WHERE id=?",
                          (*fields.values(), item_id))

    def claim_morpheus_action(self, item_id: str, action: str) -> bool:
        """Record the action ONCE: False when another pass already took it"""
        cur = self.conn.execute(
            "UPDATE morpheus_items SET action=?, action_ts=?, updated_ts=? "
            "WHERE id=? AND action=''", (action, db.now(), db.now(), item_id))
        return cur.rowcount == 1

    def claim_morpheus_auto_requeue(self, item_id: str, limit: int) -> bool:
        """Spend one automatic requeue: False once the item has spent `limit` of them"""
        cur = self.conn.execute(
            "UPDATE morpheus_items SET auto_requeues=auto_requeues+1, updated_ts=? "
            "WHERE id=? AND auto_requeues<?", (db.now(), item_id, limit))
        return cur.rowcount == 1

    # -- os state

    def set_state(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO os_state (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def get_state(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM os_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_base_health(self, project: str, fact: dict[str, Any]) -> None:
        """Record whether this project's default branch is red (docs/superpowers/specs/2026-09-26-a-red-default-branch-raises-itself.md §2)"""
        self.set_state(base_health_key(project), json.dumps(fact))

    def base_health(self, project: str) -> dict[str, Any]:
        """The stored reading, or `{}` for "never read" — which is neither red nor green"""
        raw = self.get_state(base_health_key(project))
        if not raw:
            return {}
        try:
            fact = json.loads(raw)
        except ValueError:
            return {}
        return fact if isinstance(fact, dict) else {}

    def set_base_red_episodes(self, project: str,
                              episodes: Sequence[Any]) -> None:
        """This project's red-base episodes, as `[[start, end-or-null], ...]` (docs/superpowers/specs/2026-10-15-a-merge-held-by-a-red-base-is-not-stuck.md)"""
        self.set_state(base_red_episodes_key(project),
                       json.dumps([list(p) for p in episodes]))

    def base_red_episodes(self, project: str) -> list[list[float | None]]:
        """The stored episodes, or `[]` — absent, unparseable or not a list of pairs"""
        raw = self.get_state(base_red_episodes_key(project))
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(parsed, list):
            return []
        out: list[list[float | None]] = []
        for pair in parsed:
            if not isinstance(pair, list) or len(pair) != 2:
                return []
            start, end = pair
            if isinstance(start, bool) or not isinstance(start, (int, float)):
                return []
            if end is not None and (isinstance(end, bool)
                                    or not isinstance(end, (int, float))):
                return []
            out.append([float(start), None if end is None else float(end)])
        return out
