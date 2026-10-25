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
.venv/Scripts/python code/tools/build_index.py            # writes cache/index.db (about 1 s)
.venv/Scripts/python code/tools/build_index.py --force    # unconditional rebuild
```
Code: `code/tools/build_index.py` (CLI), plus `code/stages/evidence_index/`: `normalize.py` (identifiers, record types, state rules), `parsers.py` (one per format), `reference.py` (CSVs), `model.py`, `schema.py`, `builder.py`.

Behaviour worth knowing:
- **Snapshot scopes.** Inventory and replica snapshots list only rows that exist, so absence is shown by omission. For example, case_01's `account_001` is ABSENT because evidence_54's tables don't contain it. `snapshot_scopes` records which system, store and time each snapshot covered, including zero-row scopes such as evidence_90, which covers 8 systems with no rows. Stage 3 uses scopes to infer ABSENT.
- **SQL safety.** Every statement is checked against a DDL/INSERT allow-list before it runs in an in-memory SQLite.
- **Images.** These are indexed only from `cache/media_extractions.json`, written by the vision-extraction step (not built yet). Until then the 11 PNGs are listed as unindexed in `evidence_files.note`.
- **PDFs.** Text comes from `pypdf`, falling back to `pdftotext`. Multi-subject registers are split into one observation per row. Single-subject documents become one observation per referenced resource. Alias references (`NST-…`) are resolved through `resource_aliases`.
- **Background noise.** There are 300 `profile_service` log lines with 5-digit `account_NNNNN` ids. They reference 300 distinct non-request users and are indexed under `system_id=profile_service`.
- **Determinism.** Two forced builds produce byte-identical table contents (`built_at` excluded).

Sample check: two-hop retrieval over the index (identifier → observation → resource, plus resource links) recovers **84/111 (76%)** of gold resources across the 32 samples. Every recovered resource's latest pre-audit `derived_state` agrees with its gold treatment. All 27 misses are resources that carry **no subject identifier in any evidence at or before `audit_at`**: deleted accounts and anonymized analytics, known only by id. One of them (`account_3866`, case_27) does carry identifiers, but only in a snapshot captured after the audit time, which is relevant to open decision 2 in §8.

### 2.6 Stages 1-2 as built (`code/main.py`)
- **Stage 1 (intent).** One Claude call per *distinct* request text: 375 for the 518 evaluation cases. The model is `claude-opus-5-5` at `effort: low`, with structured JSON output (`intent`, `request_type`, `language`, `rationale`) and `fallbacks: "default"`. No sampling parameters are sent; the API rejects them as of 2026-10-21. Determinism comes from the fixed prompt plus the cache: `cache/intent_cache.json`, keyed by SHA-256 of prompt version, model and text. Only successful verdicts are cached, so refusals, truncations, invalid JSON and API errors become `UNCLASSIFIED` with a flag and are retried on the next run. Missing credentials degrade to `UNCLASSIFIED` (`NO_CREDENTIALS`) instead of failing. The request text is wrapped in `<request_text>` tags and treated as data. A verdict whose `intent` disagrees with its `request_type` is flagged `INTENT_TYPE_MISMATCH`.
- **Stage 2 (identity).** The request email is matched exactly and case-insensitively against current and historical emails. That yields the `user_id`, then every identifier with its validity window. Flags:
  - `SUBJECT_NOT_RESOLVED`
  - `AMBIGUOUS_SUBJECT`: the email belongs to more than one user, so none is picked
  - `REQUEST_EMAIL_HISTORICAL`
  - `REQUEST_EMAIL_CHANGED_AFTER_REQUEST`
  - `IDENTIFIER_CHANGED_AFTER_AUDIT`
  - `REPEAT_SUBJECT`: other cases about the same person, listed in `related_cases`

  Names are kept for display but never used for matching. On the evaluation set: 418 current and 100 historical request emails, 0 unresolved, 1 repeat subject (`user_18`: case_33 and case_52).
- `NOT_DELETION` cases skip stage 2. They become `NOT_APPLICABLE, []` in stage 6. The per-case result is written to `cache/case_results.jsonl`.

### 2.7 Code layout (as built)
```
code/
├── main.py              entry point (CLI only)
├── pipeline/            main helpers: the driver (stages 0-7 in order) and the output gates
├── lib/                 shared libraries: config, api, logger
├── stages/              one module per stage (+ the evidence_index package for stage 0)
├── tools/               standalone utilities: build_index.py, inspect_case.py
└── evaluation/          evaluation and usage reports
```
Imports are package-qualified from `code/` (for example `from stages.identity import resolve_identities` or `from lib import api`). Entry scripts put `code/` on `sys.path`.

| File | Role |
|---|---|
| `code/main.py` | CLI entry point only (`--samples`, `--cases`, `--no-model`, `--verbose`, `--allow-unclassified`) |
| `code/pipeline/driver.py` | **Driver**: runs stages 0-7 in order, enforces the gates, decides where predictions are written |
| `code/lib/config.py` | Repository-relative paths |
| `code/lib/api.py` | Claude access: `.env` loading, credentials check, client, `structured_call` (refusal/truncation/error handling and usage logging in one place) |
| `code/lib/logger.py` | Console messages, run log, usage log, JSONL artefacts, run summary, evaluation printout |
| `code/stages/evidence_index/` | Stage 0 (index builder package) |
| `code/stages/intent_classifier.py` | Stage 1 (prompt, schema, cache, `classify_intents`) |
| `code/stages/identity.py` | Stage 2 (`resolve_identities`) |
| `code/stages/retrieval.py` | Stage 3 (`retrieve` → case bundle) |
| `code/stages/state_report.py` | Stage 4 contract (state-report schema, policy end states, compliance) and the deterministic **fallback** report used until the agent exists |
| `code/stages/validator.py` | Stage 5 (`validate_report` against the bundle and rules, `validate_row` against the output contract) |
| `code/stages/steps.py` | Stage 6 (`generate_steps`, `serialize_steps`) |
| `code/stages/output.py` | Stage 7 (cross-case checks, `write_predictions`, `evaluate`) |
| `code/tools/build_index.py`, `code/tools/inspect_case.py` | Rebuild the index by hand; print one case's retrieval result |

**Fail-loud gates.**
- **Stage 1 gate.** If any request is `UNCLASSIFIED` (no credentials, `--no-model` with no cached verdict, refusal, API error), the run **stops** after stage 1. It prints an error banner listing the reasons and cases, logs to `cache/run_log.jsonl`, and exits with code 2. `--allow-unclassified` exists for debugging only: it continues, treating those requests as deletions.
- **Submission gate.** A full run writes `output.csv` only when every request is classified, every case passes stage-5 validation, and there is exactly one unique row per request. Otherwise the rows go to `cache/unverified_output.csv`, a banner explains why, and the run exits with code 2.
- **Run log.** Every run logs start and end events to `cache/run_log.jsonl`: mode, case count, status counts, problems, model calls and tokens.

Where predictions go: a full run writes `output.csv` at the repo root, `--samples` writes `cache/sample_predictions.csv` (plus `cache/sample_evaluation.json`), and `--cases` writes `cache/partial_predictions.csv`. Every run also writes `cache/case_results.jsonl` with the full per-case trace: intent, identity, report, validation errors and flags.

**Sample results with the fallback report (no agent yet):** status accuracy 1.000, step precision 0.976, recall 0.850, F1 0.909, field accuracy 0.747, sequence accuracy 0.312. Every missed step except two cases belongs to the agreed unlinkable resources. Wherever all of a case's resources are linked, step order matches gold exactly. The remaining fallback errors are adjudication calls left for the agent:
- case_12: `analytics_9914` is a contact record found by an old email hash; gold deletes it, the fallback anonymizes it.
- case_17: the extra older account `account_6523`.

### 2.8 Stage 4 as built (`code/stages/adjudicator/`)
- **Framework.** Strands Agents 1.62 (`strands-agents[anthropic]`) on `claude-opus-5-5`, effort `medium`, `fallbacks: "default"`. Strands pins `anthropic<1.0`, so the SDK is pinned to `anthropic==0.125.0`; it supports every parameter stage 1 uses.
- **When it runs (`selection.py`).** Only cases whose bundle has an open question go to the agent:
  - unknown data category (support message vs attachment);
  - an analytics record that is neither identifiable nor anonymized;
  - more than one linked account;
  - a catalog-only link;
  - newest evidence disagreeing with the newest citable evidence;
  - uncitable state in a case that would be COMPLETE;
  - a violation the fallback had to exclude.

  Post-audit links are *not* sent to the agent, because retrieval already applies the decided rule. Flagged: 5/32 samples, about 60/518 evaluation cases. Everything else keeps the deterministic draft report.
- **Tools (`tools.py`).** Read-only, bound to one case, every query cut at `audit_at`:
  - `get_resource_timeline`
  - `read_evidence` (parsed record plus raw log lines, hiding lines after `audit_at`)
  - `get_trace`
  - `search_identifier` (exact; shows the owner, to rule out lookalikes)
  - `submit_state_report`, which runs the stage-5 validator and returns errors, so the agent corrects its report within the same loop (at most 4 submissions)

  Each tool call opens its own read-only SQLite connection, because Strands runs tools on its own threads. Tool failures are recorded and flagged (`AGENT_TOOL_ERRORS`).
- **Runner (`runner.py`).** One agent per case, 4 cases in parallel. Limits: 20 turns and 400k tokens per case. Accepted reports are cached in `cache/adjudication_cache.json`, keyed by prompt version, model, effort, compact bundle and questions. Token usage goes to `cache/usage_log.jsonl`. No accepted report means the case keeps its draft report, is flagged, and a stage-4 error banner is shown.
- **CLI.** `--agent flagged` (default), `all`, or `off`, plus `--agent-workers N`.
- **Sample results with the agent:** status 1.000, step precision 0.994, recall 0.865, F1 0.925, field accuracy 0.755, sequence accuracy 0.344. Every agent report was accepted on the first submission in 4-5 turns, at about $0.12 per case.
  - The agent fixed case_12: `analytics_9914` is a contact lookup keyed by an old email hash, so it is `SECONDARY_IDENTIFIER` and deleted.
  - It confirmed case_07's attachment.
  - Remaining non-gold step: case_17's older account `account_6523`, which is correctly linked but not in gold.

### 2.9 Independent evaluation: OpenAI single-call judge (`code/evaluation/`)
- **What it does.** `python code/evaluation/main.py` reviews the latest pipeline run (`cache/case_results.jsonl`). It makes one OpenAI Responses API call per case (`gpt-6.1-sol`, reasoning effort `low`) with a strict Pydantic schema. The judge sees:
  - the request, the intent verdict, and the evidence bundle cut at `audit_at`;
  - our per-resource decision basis (category, states, rationale);
  - our status and steps.

  It returns per-step agree/disagree/unsure with reasons, any missing steps, the status it expects, and a case verdict. It is advisory and never changes `output.csv`. A different provider from the pipeline avoids self-agreement bias.
- **Cost and caching.** About $0.008 per case: 32 samples cost $0.26, so all 518 rows would be about $4. Verdicts are cached in `cache/judge_cache.json`, keyed by prompt version, model, effort and payload. Usage goes to `cache/usage_log.jsonl` with `provider: openai`, and the summary goes to `cache/judge_summary.json`.
- **Calibration against gold (samples).** Sample runs score the judge itself against gold.
  - **Without the decision basis (judge-v1),** it disagreed with case_12's correct reclassification, which gold agrees with.
  - **With it (judge-v2),** it agreed with all 168 steps: 167 are correct by gold, and it missed the one wrong step (case_17's extra account).
  - It cannot see unlinkable resources, so it finds none of the 26 gold steps we omit.

  Conclusion so far: the judge confirms our outputs are well supported, but its ability to catch errors is still unmeasured. There is only one wrong step in the samples. Next step: a mutation test that injects known errors into sample outputs and measures how many the judge catches.

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

## 8. Decisions
1. **DECIDED: resources with no identifier link are not guessed.** A resource enters a case only through a proven link: an exact identifier, a trace, a query, a resource link, or a catalog claim (flagged). Records that can't be linked, such as deleted accounts known only by id, are left out, and the case is flagged `NO_ACCOUNT_RESOURCE_LINKED`. Ranking candidates by activity timing was rejected because it is rarely unique (4–68 candidates per window) and risks naming another person's record.
2. **DECIDED: post-audit evidence may be used for linkage only, and only for records that already existed at the audit time.** An observation captured after `audit_at` may establish *that a resource belongs to the subject* (`link_basis=post_audit_identifier`), but only if the resource **also has its own observation at or before `audit_at`**. Later existence doesn't prove earlier existence: in this domain records are routinely created or recreated after the fact by re-signups, connector repopulation and restores. Post-audit evidence never contributes to state and is never cited. This recovers case_27's `account_3866`, which had a pre-audit 404. It excludes case_33's `account_2373`, `search_2352` and `audit_record_8486`, which first appear on 2026-09-15 against an audit of 2026-07-19. Excluded resources are listed in the bundle's `excluded` field with the reason.
3. Open: the retry budget N and fallback policy, to be tuned against the samples.
4. **DECIDED (fix round, see [evaluation_analysis.md](evaluation_analysis.md)): catalog claims count only inside the request window.** An unverified data-catalog ownership claim links a resource only if it was recorded between `requested_at` and `audit_at`. All 12 catalog claims fall inside exactly one case's window. The stale out-of-window matches are sample cases whose gold excludes the resource.
5. **DECIDED: citation by state class.** PRESENT and IDENTIFIABLE are one "still holds data" class when choosing citable evidence. Aliases naming several resources resolve to the one whose system matches the evidence file's origin, if exactly one does.
6. **DECIDED: judge model.** GPT-6.1 Sol at `low` effort: 0/32 false alarms and 61/61 planted errors caught, at about 1/5 of GPT-6 Astra's cost.
