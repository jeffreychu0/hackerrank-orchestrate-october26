"""SQLite schema for the evidence index (see docs/pipeline.md section 2)."""

SCHEMA_VERSION = "1"

DDL = """
PRAGMA journal_mode = OFF;
PRAGMA synchronous = OFF;

CREATE TABLE build_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Manifest of participant evidence files.
CREATE TABLE evidence_files (
    file_id      TEXT PRIMARY KEY,
    file_path    TEXT NOT NULL,
    origin       TEXT NOT NULL,
    captured_at  TEXT NOT NULL,
    source_kind  TEXT NOT NULL,          -- log | json | sql | txt | pdf | image
    indexed      INTEGER NOT NULL,       -- 1 if parsed into observations
    note         TEXT
);

-- One row per fact: "at obs_time, file F saw resource R in system S with these fields".
CREATE TABLE observations (
    obs_id             INTEGER PRIMARY KEY,
    file_id            TEXT NOT NULL REFERENCES evidence_files(file_id),
    file_captured_at   TEXT NOT NULL,    -- governs CITATION (must be <= audit_at to cite)
    obs_time           TEXT NOT NULL,    -- governs STATE (most specific timestamp available)
    obs_time_source    TEXT NOT NULL,    -- record | snapshot | line | header | manifest
    system_id          TEXT,
    resource_id        TEXT,             -- NULL only for alias-only document facts
    record_type        TEXT,
    record_type_source TEXT NOT NULL,    -- explicit | collection | event | prefix | unknown
    store_role         TEXT NOT NULL,    -- primary | read_replica | failover_replica | regional_replica:<r>
                                         -- | reporting_replica | inventory | snapshot | readback | log | document | image
    derived_state      TEXT NOT NULL,
    container          TEXT,             -- table / collection / event name
    trace_id           TEXT,
    fields_json        TEXT NOT NULL,
    source_ref         TEXT NOT NULL     -- path + line / JSON path / table+row, for excerpts
);
CREATE INDEX ix_obs_resource ON observations(resource_id, obs_time);
CREATE INDEX ix_obs_trace ON observations(file_id, trace_id);
CREATE INDEX ix_obs_time ON observations(obs_time);

-- Inverted index: identifier -> observation.
CREATE TABLE observation_identifiers (
    obs_id      INTEGER NOT NULL REFERENCES observations(obs_id),
    identifier  TEXT NOT NULL,           -- normalised
    id_kind     TEXT NOT NULL,           -- email | email_hash | phone | user_ref | name
    field       TEXT NOT NULL,           -- source field, or the lookup key field for trace links
    link_via    TEXT NOT NULL,           -- direct | trace | query | document
    PRIMARY KEY (obs_id, identifier, id_kind, field, link_via)
);
CREATE INDEX ix_oi_identifier ON observation_identifiers(identifier, id_kind);

-- What each snapshot covered, so absence-by-omission can be inferred at query time.
CREATE TABLE snapshot_scopes (
    file_id      TEXT NOT NULL,
    system_id    TEXT NOT NULL,
    store_role   TEXT NOT NULL,
    container    TEXT NOT NULL,
    captured_at  TEXT NOT NULL,
    scope_kind   TEXT NOT NULL,          -- table_snapshot | collection_snapshot | query_export
    row_count    INTEGER NOT NULL,
    PRIMARY KEY (file_id, system_id, store_role, container)
);

-- Alternative references for a resource (order numbers, ticket ids, statement ids, file names).
CREATE TABLE resource_aliases (
    alias        TEXT NOT NULL,
    alias_kind   TEXT NOT NULL,
    resource_id  TEXT NOT NULL,
    source       TEXT NOT NULL,          -- file_id or activity_id
    PRIMARY KEY (alias, resource_id, source)
);

-- Resource-to-resource relations (connector/restore repopulation, search doc -> source account).
CREATE TABLE resource_links (
    resource_id          TEXT NOT NULL,
    related_resource_id  TEXT NOT NULL,
    relation             TEXT NOT NULL,  -- repopulated | restored_into | indexed_from
    source               TEXT NOT NULL,
    event_time           TEXT,
    PRIMARY KEY (resource_id, related_resource_id, relation, source)
);

-- system_activity.csv: claims, not proof.
CREATE TABLE activity (
    activity_id          TEXT PRIMARY KEY,
    seq                  INTEGER NOT NULL,
    event_time           TEXT NOT NULL,
    event_type           TEXT NOT NULL,
    source_system        TEXT NOT NULL,
    target_system        TEXT NOT NULL,
    resource_id          TEXT NOT NULL,
    reported_result      TEXT NOT NULL,
    details              TEXT NOT NULL,
    downstream_resource_id TEXT,
    linked_alias         TEXT,
    linked_identifier    TEXT,           -- normalised email from LINK_RESOURCE_IDENTITY claims
    linked_user_id       TEXT,
    exclusive_subject    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX ix_act_resource ON activity(resource_id, event_time);
CREATE INDEX ix_act_downstream ON activity(downstream_resource_id);
CREATE INDEX ix_act_time ON activity(event_time);

-- Identity snapshot + audit history, one row per (identifier, user).
CREATE TABLE identities (
    identifier   TEXT NOT NULL,
    id_kind      TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    is_current   INTEGER NOT NULL,
    valid_from   TEXT,
    valid_to     TEXT,
    source_id    TEXT NOT NULL,          -- 'snapshot' or audit_id
    PRIMARY KEY (identifier, id_kind, user_id, source_id)
);
CREATE INDEX ix_ident_user ON identities(user_id);

CREATE TABLE deletion_rules (
    rule_id          TEXT PRIMARY KEY,
    data_category    TEXT NOT NULL,
    condition        TEXT NOT NULL,
    required_action  TEXT NOT NULL
);

CREATE TABLE requests (
    case_id          TEXT PRIMARY KEY,
    is_sample        INTEGER NOT NULL,
    email            TEXT NOT NULL,
    email_norm       TEXT NOT NULL,
    requested_at     TEXT NOT NULL,
    reported_status  TEXT NOT NULL,
    reported_at      TEXT NOT NULL,
    audit_at         TEXT NOT NULL,
    request_text     TEXT NOT NULL
);

-- Identifier -> user resolution for observations (names excluded: not unique).
CREATE VIEW observation_subjects AS
SELECT oi.obs_id, i.user_id, oi.identifier, oi.id_kind, oi.field, oi.link_via, i.is_current, i.source_id
FROM observation_identifiers oi
JOIN identities i ON i.identifier = oi.identifier AND i.id_kind = oi.id_kind
WHERE oi.id_kind <> 'name';
"""
