CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    project TEXT NOT NULL,
    wo_id TEXT NOT NULL,
    question TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    kind TEXT NOT NULL DEFAULT 'question',   -- question | approval
    answer TEXT,
    answered_by TEXT,                        -- neo | user
    answer_reason TEXT,                      -- Neo's stated reasoning / escalation reason
    answered_at REAL,
    review_status TEXT NOT NULL DEFAULT 'unreviewed',
    review_feedback TEXT,
    reviewed_at REAL
);
CREATE TABLE IF NOT EXISTS learnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    project TEXT NOT NULL DEFAULT '',        -- '' = applies everywhere
    content TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',   -- review | escalation | manual
    question_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_q_status ON questions(status);
CREATE INDEX IF NOT EXISTS idx_q_review ON questions(review_status);
CREATE INDEX IF NOT EXISTS idx_learn_project ON learnings(project);
