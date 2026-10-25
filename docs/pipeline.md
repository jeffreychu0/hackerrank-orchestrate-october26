# Pipeline Architecture: Ghost Data Still Exists

Design goal: **models only make decisions that need judgement. Everything mechanical is deterministic Python.** Related docs: [dataset_analysis.md](dataset_analysis.md), [sample_case_analysis.md](sample_case_analysis.md).

## 1. Stages

| # | Stage | Executor | Input → Output |
|---|---|---|---|
| 0 | **Build the evidence index** (once per dataset) | Python. A vision model runs once for the 11 PNG + 4 PDF files, and the results are cached. | `dataset/` → `cache/index.db` (SQLite) |
| 1 | **Split cases and classify intent** | Small model call, temperature 0. The result is cached per `case_id`. | request row → `DELETION` / `NOT_DELETION`. `NOT_DELETION` short-circuits to `NOT_APPLICABLE, []`. |
| 2 | **Resolve identity** | Python | request email → `user_id` → full identifier set (current and historical) |
| 3 | **Retrieve candidates** | Python (index queries) | identifiers + `audit_at` → **case bundle**: every linked resource, its timeline, and its latest state |
| 4 | **Adjudicate** | Strands agent with index tools | case bundle → **state report**. The agent resolves only the items flagged ambiguous. |
| 5 | **Validate** | Python | state report → `VALID`, or a list of specific errors (fed back to stage 4, at most N retries, then a deterministic fallback) |
| 6 | **Generate steps** | Python (rules table + ordering rules) | validated state report → `deletion_status`, `steps_json` |
| 7 | **Run cross-case checks, write the output, evaluate** | Python | all cases → `output.csv`, metrics against `sample_output.csv` |

The model touches stages 1 and 4, plus the one-time extraction of images and PDFs in stage 0. Status, step operations, step order, numbering and evidence-id blanking are never written by a model.

## 2. Stage 0: the evidence index

### 2.1 Why build it once
The corpus is finite (94 files plus 4,011 activity rows), and every case searches the same corpus. Parsing once means:
- every case sees the same parsed facts;
- per-case token cost is small;
- decoy and time traps are handled by code instead of by the model's attention.

The index is **timeless and global**. It records every observation with its own timestamp. **No case's `audit_at` is baked into it.** The cutoff is applied at query time (stage 3), so one cache serves all 518 cases.

### 2.2 The unit of storage: an observation
One observation is "at time *T*, source *F* saw resource *R* in system *S* with these fields". Parsers emit one observation per log event, JSON record, SQL row, lookup line, or image/PDF fact.

```
observations
  obs_id            INTEGER PK
  file_id           TEXT    -- evidence_NN (citable id); NULL for activity-derived rows
  file_captured_at  TEXT    -- manifest captured_at (governs CITATION)
  obs_time          TEXT    -- the observation's own timestamp (governs STATE); see 2.4
  system_id         TEXT    -- logical service: account_service, search_service, ...
  resource_id       TEXT    -- account_5334, search_9359, data_flow_8524, ...
  record_type       TEXT    -- category hint: USER_PROFILE, SUPPORT_MESSAGE, ... (NULL if unknown)
  store_role        TEXT    -- primary | read_replica | failover_replica | regional:<r> | reporting | readback | claim
  derived_state     TEXT    -- PRESENT | ABSENT | IDENTIFIABLE | ANONYMIZED | REDACTED | ACTIVE | DISABLED | BLOCKED | RETAINED | UNKNOWN
  fields_json       TEXT    -- normalised raw fields (email, phone, email_hash, customer_reference, enabled, restore_allowed, http_status, rows, ...)
  trace_id          TEXT    -- for log events
  source_ref        TEXT    -- file path + line number / JSON path / SQL table+row, for excerpts

observation_identifiers          -- inverted index: identifier -> observation
  identifier  TEXT  -- normalised: lowercase+trimmed email, digits-only phone, lowercase hash, user_NN
  id_kind     TEXT  -- email | phone | email_hash | user_ref | name
  obs_id      INTEGER
  INDEX (identifier)

activity                          -- system_activity.csv, parsed
  activity_id, event_time, event_type, source_system, target_system,
  resource_id, reported_result, details,
  downstream_resource_id   -- parsed from details: "repopulated downstream resource search_4882"
  linked_identifier, linked_user_id   -- parsed from LINK_RESOURCE_IDENTITY rows (catalog claims)

identities                        -- user_identities.csv + user_audit_log.csv
  identifier, id_kind, user_id, valid_from, valid_to, source_id (audit_NN or 'snapshot')

INDEX observations(resource_id), observations(trace_id), activity(resource_id), activity(downstream_resource_id)
```

### 2.3 Build-time normalisation that removes ambiguity
| Problem in the raw data | What the builder does |
|---|---|
| A log lookup splits identity and result across lines (`*_started lookup_key=…` / `*_completed resource_id=… rows=0`) | Group by `trace_id`, then copy the lookup key's identifiers onto the completed observation. This keeps 404 results linkable. |
| Emails differ in case (`CHOI.JIHOON94@GMAIL.COM`) | Lowercase and trim every email. Normalise phones to digits only. |
| `.jr` / prefix lookalikes | Match identifiers **exactly** only. `g.silva76.jr@gmail.com` is a different key from `g.silva76@gmail.com`. |
| Replica tables in SQL snapshots | One observation per table row, with `store_role` set. A resource is PRESENT if **any** store has it. |
| Misleading file names / origins | Take the category from the `record_type`/`system` fields and the table/collection names, never from the file name. |
| Raw fields → state | A fixed mapping, for example `http_status=404 / rows=0 / NOT_FOUND → ABSENT`; `enabled=true → ACTIVE`; `restore_allowed=false → BLOCKED`; analytics with all identifier fields null → `ANONYMIZED`; support ticket with `customer_reference` but no requester fields → `REDACTED`. |
| Repopulation hides the real record in `details` | Parse `downstream_resource_id` so a data flow links to the search record it recreated. |
| Images/PDFs | A vision model extracts them once, with a strict schema, into the same observation shape (`source_kind=image/pdf`). The result is cached and reviewed by hand, since there are only 15 files. |

### 2.4 Two timestamps per observation
- **`obs_time`** (used for state) is the most specific timestamp available, in this order: the record's own `observed_at`/`recorded_at`, then the enclosing snapshot's `captured_at`, then the log line timestamp, then the SQL header `Captured at`, then the manifest `captured_at`.
- **`file_captured_at`** (used for citation) is the manifest value. A finding or a COMPLETE-case verification may cite a `file_id` only if `file_captured_at ≤ audit_at`.

Why both: `customer_data_snapshot_bundle_20260917.json` is filed on 09-17. One snapshot inside it was captured 2026-09-15T12:00:00Z and holds `audit_record_1707 → user_30`, which case_30 (audit 09-15T12:00) needs. Another snapshot inside it, captured 09-17, shows case_27's account back to ACTIVE, which must be ignored. Filtering by file date gets both wrong.

### 2.5 As built (stage 0 implementation)
Run once per machine. It rebuilds automatically when the dataset, the media extraction, or the builder code changes:
```
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt   # Windows (use .venv/bin/ on macOS/Linux)
.venv/Scripts/python code/build_index.py            # writes cache/index.db (about 1 s)
.venv/Scripts/python code/build_index.py --force    # unconditional rebuild
```
Code: `code/build_index.py` (CLI), plus `code/evidence_index/`: `normalize.py` (identifiers, record types, state rules), `parsers.py` (one per format), `reference.py` (CSVs), `model.py`, `schema.py`, `builder.py`.

Behaviour worth knowing:
- **Snapshot scopes.** Inventory and replica snapshots list only rows that exist, so absence is shown by omission. For example, case_01's `account_001` is ABSENT because evidence_54's tables don't contain it. `snapshot_scopes` records which system, store and time each snapshot covered, including zero-row scopes such as evidence_90, which covers 8 systems with no rows. Stage 3 uses scopes to infer ABSENT.
- **SQL safety.** Every statement is checked against a DDL/INSERT allow-list before it runs in an in-memory SQLite.
- **Images.** These are indexed only from `cache/media_extractions.json`, written by the vision-extraction step (not built yet). Until then the 11 PNGs are listed as unindexed in `evidence_files.note`.
- **PDFs.** Text comes from `pypdf`, falling back to `pdftotext`. Multi-subject registers are split into one observation per row. Single-subject documents become one observation per referenced resource. Alias references (`NST-…`) are resolved through `resource_aliases`.
- **Background noise.** There are 300 `profile_service` log lines with 5-digit `account_NNNNN` ids. They reference 300 distinct non-request users and are indexed under `system_id=profile_service`.
- **Determinism.** Two forced builds produce byte-identical table contents (`built_at` excluded).

Sample check: two-hop retrieval over the index (identifier → observation → resource, plus resource links) recovers **84/111 (76%)** of gold resources across the 32 samples. Every recovered resource's latest pre-audit `derived_state` agrees with its gold treatment. All 27 misses are resources that carry **no subject identifier in any evidence at or before `audit_at`**: deleted accounts and anonymized analytics, known only by id. One of them (`account_3866`, case_27) does carry identifiers, but only in a snapshot captured after the audit time, which is relevant to open decision 2 in §8.

## 3. Stage 3: per-case retrieval (how a case pulls "all of my data")

This shows exactly how the index answers a case.

```
retrieve(case):
  # 1. Identity: one lookup
  user  = identities[case.email.lower()]              -> user_30
  ids   = all identifiers of user                      -> 2 emails, 2 hashes, 1 phone, user_30, name

  # 2. Hop 1, identifier -> observations (inverted index), cut off at audit_at
  seed  = observation_identifiers[ids] WHERE obs_time <= audit_at
        + activity WHERE linked_user_id = user          (catalog claims, flagged UNVERIFIED)

  # 3. Hop 2, observation -> resource -> FULL resource timeline
  R     = {o.resource_id for o in seed}
  R    += {a.downstream_resource_id for a in activity WHERE resource_id IN R}   # flow -> recreated record
  for r in R:
      timeline[r] = observations WHERE resource_id = r AND obs_time <= audit_at
                  + activity     WHERE resource_id = r AND event_time <= audit_at
      latest[r]   = state of the newest non-claim observation, where
                    any store PRESENT beats ABSENT at the same snapshot

  # 4. Flags for the agent
  flag  r  if: category unresolved | timeline contradicts itself | catalog-only link
              | identifier matched only by a stale hash | no evidence observation at all
  flag case if: no account_service resource found (known gap)
  return case bundle
```

**Hop 2 is the key idea.** Records that were deleted or anonymized have lost their identifiers. They are found because the same `resource_id` showed up earlier with identifiers, or a trace tied a lookup key to it. Once a resource is linked, its whole history comes with it, including identifier-free rows like `{"resource_id": "account_5334", "status_code": 404}`.

### 3.1 Worked example: case_30 (from real data)
Request: `camille.bernard86@outlook.com`, audit_at `2026-09-15T12:00:00Z`.

1. **Identity.** `camille.bernard86@outlook.com` is a historical email (`audit_280`) of `user_30`. The identifier set is `{camille.bernard@outlook.com, camille.bernard86@outlook.com, 81310fd2…, b891c158…, 33657326206, user_30, "camille bernard"}`.
2. **Hop 1** (identifier hits with `obs_time ≤ audit_at`):
   | Identifier matched | Observation | Source |
   |---|---|---|
   | old email | `search_9359` hit in index query | evidence_92 |
   | old email | `analytics_3697` profile with `email` populated | evidence_92 |
   | old email | `support_6477` ticket with `requester_email` populated | evidence_92 |
   | old email | `data_flow_8524` connector `customer_email`, `enabled: true` | evidence_92 |
   | `user_30` | `audit_record_1707` (`subject_reference`) | evidence_02 (09-17 bundle), inner snapshot `captured_at` 09-15T12:00. Usable for state, not citable. |
3. **Hop 2** pulls each resource's full timeline. For example, `data_flow_8524` gains any `CHECK_DATA_FLOW` activity, and the latest state stays ACTIVE.
4. **Case bundle returned to the agent** (abridged):
   ```json
   {"case_id":"case_30","user_id":"user_30","audit_at":"2026-09-15T12:00:00Z",
    "resources":[
     {"resource_id":"search_9359","system_id":"search_service","category":"SEARCH_INDEX_ENTRY","latest_state":"PRESENT","citable":["evidence_92"],"flags":[]},
     {"resource_id":"analytics_3697","system_id":"analytics_platform","category":"ANALYTICS_PROFILE","latest_state":"IDENTIFIABLE","citable":["evidence_92"],"flags":[]},
     {"resource_id":"support_6477","system_id":"support_platform","category":null,"latest_state":"IDENTIFIABLE","citable":["evidence_92"],"flags":["CATEGORY_UNRESOLVED: message vs attachment"]},
     {"resource_id":"data_flow_8524","system_id":"integration_service","category":"DATA_FLOW","latest_state":"ACTIVE","citable":["evidence_92"],"flags":[]},
     {"resource_id":"audit_record_1707","system_id":"security_service","category":"SECURITY_AUDIT_LOG","latest_state":"RETAINED","citable":[],"flags":["NOT_CITABLE: file captured after audit_at"]}],
    "case_flags":["NO_ACCOUNT_RESOURCE_LINKED"]}
   ```
5. The agent only has to settle `support_6477`'s category (a shared delivery conversation, so REDACT) and decide how to handle the missing account resource. Everything else is already settled.

`account_4163` (in the gold answer) appears only as a `DELETE_RECORD` activity row with no identifier. This is the known linkage gap ([sample_case_analysis.md §6](sample_case_analysis.md)). The bundle surfaces it as a case flag so it is never silently guessed.

## 4. Stage 4: the Strands agent

**Inputs:** the request text, the case bundle, and `deletion_rules.csv`.

**Tools** (all read-only queries against `index.db`, all enforcing `audit_at`):
| Tool | Purpose |
|---|---|
| `get_case_bundle(case_id)` | The stage-3 output. This is the agent's starting point. |
| `get_resource_timeline(resource_id)` | Every observation and activity row for one resource, at or before the audit time |
| `search_identifier(value)` | Exact-match lookup for an identifier the agent wants to test. Never fuzzy. |
| `get_trace(trace_id)` | All log events in one trace |
| `read_excerpt(obs_id)` | The raw source lines/JSON for one observation, for judging context (for example, support message text) |
| `get_activity(resource_id)` | Activity claims for a resource, including downstream links |
| `submit_state_report(report)` | Final structured output (validated by Pydantic before stage 5) |

**The agent may not:** read raw files, see observations after `audit_at`, write steps, or set the final status.

**State report schema** (one entry per resource):
```
resource_id, system_id, category (deletion_rules data_category or DATA_FLOW),
observed_state (from the enum), required_state (from rules), compliant (bool),
evidence_id (citable file_id or null), link_basis (identifier | trace | resource_hop | catalog),
rationale (one sentence)
```

## 5. Stage 5: the validator
Checks, each returning a specific error message:
- the schema is valid and enums are legal;
- every bundle resource appears in the report (**guards recall**), and no resource was invented;
- `required_state` matches `deletion_rules.csv` for the category;
- `compliant` is consistent with `observed_state` vs `required_state`;
- every cited `evidence_id` has `file_captured_at ≤ audit_at` and actually contains that `resource_id`;
- `link_basis` is an exact identifier, trace, or resource hop. A catalog-only link needs corroborating evidence.
- a non-deletion intent must have an empty report.

**On failure:** send the errors back to the agent (maximum 2 retries). If it still fails, use the deterministic fallback, which is the bundle's `latest_state` plus a rules-table category guess.

## 6. Stage 6: step generation (deterministic)
- `INCOMPLETE` if any resource is non-compliant, otherwise `COMPLETE`.
- Map each violation to its FINDING, REMEDIATION and VERIFICATION using the table in [sample_case_analysis.md §3.3](sample_case_analysis.md).
- Order the steps by §3.4 of the same doc. Number them from 1. Set `evidence_id` to `""` for remediation steps and for all INCOMPLETE verifications.
- Serialise JSON with fixed key order and no whitespace variance.

## 7. Stage 7: cross-case checks and evaluation
- A resource linked to more than one `user_id` across cases is flagged for review (catches lookalike accounts).
- Write exactly 518 rows sorted by `case_id`, and validate them against the output contract.
- Run the same pipeline on the 32 samples. Report status, step, field and sequence accuracy, which feeds `evaluation/evaluation_report.md`.
- Log every model call's token usage, which feeds `evaluation/usage_report.md`.

## 8. Open decisions
1. Strategy for linking account/analytics resources that carry no identifier (§3.1, `account_4163`).
2. Whether observations captured after `audit_at` may ever be used for *linkage only*. Current design: **no**. Everything is cut at `obs_time ≤ audit_at`. The case_30 check shows this is sufficient there.
3. The retry budget N and fallback policy, to be tuned against the samples.
