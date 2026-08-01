
CREATE TABLE IF NOT EXISTS work_orders (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    origin TEXT NOT NULL DEFAULT 'manual',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    model TEXT,
    effort TEXT,
    permission_mode TEXT,
    append_system_prompt TEXT,
    session_id TEXT,
    bg_id TEXT,
    job_id TEXT,        -- supervisor job of the worker's most recent turn
    reply_job_id TEXT,  -- job whose final assistant message is already recorded
    worktree TEXT,
    branch TEXT,
    needs_attention INTEGER NOT NULL DEFAULT 0,
    attention_reason TEXT,
    result_summary TEXT,
    backlog_id TEXT,
    metadata TEXT
);
CREATE TABLE IF NOT EXISTS wo_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wo_id TEXT NOT NULL REFERENCES work_orders(id),
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT
);
CREATE TABLE IF NOT EXISTS wo_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wo_id TEXT NOT NULL REFERENCES work_orders(id),
    ts REAL NOT NULL,
    direction TEXT NOT NULL,            -- user_to_agent | agent_to_user
    content TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'jarvis',  -- jarvis | ui | direct
    status TEXT NOT NULL DEFAULT 'queued',  -- queued | delivered | failed
    delivered_at REAL
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    level TEXT NOT NULL DEFAULT 'info', -- info | warning | critical
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    wo_id TEXT,
    source TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new'  -- new | routed
);
CREATE TABLE IF NOT EXISTS assumptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wo_id TEXT NOT NULL REFERENCES work_orders(id),
    ts REAL NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' -- pending | accepted | rejected
);
-- Requests to perform a privileged action (merge a PR, cut a release). Filed by the
-- PreToolUse gate when a worker attempts one, reviewed by Neo, and consumed by the
-- retry. A row is a receipt for one command, not a capability: see gates.py.
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wo_id TEXT NOT NULL REFERENCES work_orders(id),
    ts REAL NOT NULL,
    kind TEXT NOT NULL,                     -- gates.KIND_NAMES
    command TEXT NOT NULL,                  -- the exact string the grant authorises
    matched TEXT NOT NULL DEFAULT '',       -- the recogniser that fired
    justification TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending', -- pending | approved | denied | expired
    -- Neo declined to decide, so the request is still pending but it is now the USER
    -- who holds it. This is the bit that decides whether a gate costs the user any
    -- attention: pending-with-Neo must stay silent, pending-with-user must not.
    escalated INTEGER NOT NULL DEFAULT 0,
    escalation_reason TEXT,
    neo_question_id INTEGER,
    decided_by TEXT,                        -- neo | user
    decision_reason TEXT,
    decided_at REAL,
    expires_at REAL,
    uses INTEGER NOT NULL DEFAULT 0,
    max_uses INTEGER NOT NULL DEFAULT 3
);
CREATE INDEX IF NOT EXISTS idx_wo_status ON work_orders(status);
CREATE INDEX IF NOT EXISTS idx_events_wo ON wo_events(wo_id);
CREATE INDEX IF NOT EXISTS idx_msgs_status ON wo_messages(status);
CREATE INDEX IF NOT EXISTS idx_notif_status ON notifications(status);
CREATE INDEX IF NOT EXISTS idx_approvals_wo ON approvals(wo_id, status);
