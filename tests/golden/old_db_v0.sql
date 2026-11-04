-- SmartCut v0 旧库快照（无 schema_meta、无后加列）。
-- 用于验证 Database.init() 的 v1→v4 迁移与二次执行幂等。
CREATE TABLE orchestration_runs (
    id VARCHAR(64) PRIMARY KEY,
    project_id VARCHAR(64),
    goal TEXT,
    status VARCHAR(16),
    allow_render BOOLEAN,
    manual_ir_json TEXT,
    plan_json TEXT,
    ledger_json TEXT,
    answer TEXT,
    confidence FLOAT,
    artifacts_json TEXT,
    warnings_json TEXT,
    error TEXT,
    created_at DATETIME,
    updated_at DATETIME
);

INSERT INTO orchestration_runs(id, project_id, goal, status)
VALUES ('run_old', 'p_old', '旧库运行', 'succeeded');

CREATE TABLE media_assets (
    id VARCHAR(64) PRIMARY KEY,
    project_id VARCHAR(64),
    src TEXT,
    facts_json TEXT,
    transcript_json TEXT,
    created_at DATETIME
);

INSERT INTO media_assets(id, project_id, src, facts_json)
VALUES ('m_old', 'p_old', 'media/a.mp4', '{}');

CREATE TABLE edit_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id VARCHAR(64),
    kind VARCHAR(32),
    payload_json TEXT,
    created_at DATETIME
);

INSERT INTO edit_feedback(project_id, kind, payload_json)
VALUES ('p_old', 'segment_keep', '{}');
