# Ghost Data Still Exists: Deletion-Audit Agent

For every request in `dataset/deletion_requests.csv`, this pipeline decides whether the person asked for deletion. If they did, it finds every record linked to them across the evidence (as it stood at the request's `audit_at` cutoff), applies `deletion_rules.csv`, and writes `output.csv`: `COMPLETE`, `INCOMPLETE` or `NOT_APPLICABLE`, with ordered findings, remediation steps and verification checks.

**Headline results.** On the 32 labelled samples: status accuracy **1.000**, step F1 **0.925** against a no-guess ceiling of 0.928. On the 518 evaluation cases: all rows pass validation, and an independent GPT-6.1 Sol reviewer rates all 518 valid. A full run costs about **$7.60** (about $0.015 per request).

Design and analysis docs: [`docs/pipeline.md`](../docs/pipeline.md) (architecture and decisions), [`docs/sample_case_analysis.md`](../docs/sample_case_analysis.md) (COMPLETE vs INCOMPLETE rules), [`docs/dataset_analysis.md`](../docs/dataset_analysis.md) (data model and traps), [`docs/e2e_results.md`](../docs/e2e_results.md) (clean baseline run), [`docs/evaluation_analysis.md`](../docs/evaluation_analysis.md) (fixes, F1, judge models).

---

## 1. Setup and run

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt        # Windows; use .venv/bin/ on macOS/Linux
cp .env.example .env                                 # add ANTHROPIC_API_KEY (pipeline) and OPENAI_API_KEY (evaluation)

python code/main.py --samples                        # 32 labelled samples -> cache/sample_predictions.csv + accuracy
python code/main.py                                  # 518 cases -> output.csv at the repository root
python code/evaluation/main.py                       # independent judge over the latest run
```

| Option | Effect |
|---|---|
| `--cases case_33,case_10` | Run specific cases (writes `cache/partial_predictions.csv`) |
| `--agent flagged\|all\|off` | Stage-4 agent on flagged cases (default), on every deletion case, or never |
| `--no-model` | Make no model calls; cached results are still used |
| `--verbose` | One line per case |
| `code/tools/build_index.py` | Rebuild the evidence index by hand (`main.py` does this automatically) |
| `code/tools/extract_media.py` | One-time vision extraction of the screenshot evidence |
| `code/tools/inspect_case.py case_30` | Print one case's retrieved evidence |

**Requirements:** Python 3.11+ (developed on 3.14), with `anthropic==0.125.0`, `strands-agents[anthropic]==1.62.0`, `openai==3.37.0` and `pypdf==5.1.0`. Credentials are read only from the environment or an uncommitted `.env` file, and are never logged.

---

## 2. End-to-end workflow

```
dataset/ ──► [0] Evidence index (SQLite) ──► per case:
              94 files, 2,940 observations     [1] Intent (Claude)             DELETION / NOT_DELETION
                                               [2] Identity                    request email → user_id → all identifiers
                                               [3] Retrieval                   linked records + state at audit_at
                                               [4] Adjudication                deterministic report, or Strands agent if ambiguous
                                               [5] Validation                  report vs evidence + output contract
                                               [6] Step generation             status + ordered steps, by rule
                                          ──► [7] Cross-case checks, gates, output.csv, scoring
                                          ──► Evaluation: independent OpenAI judge (advisory)
```

| Stage | Module | Who decides | What it does |
|---|---|---|---|
| 0 | `stages/evidence_index/` | Code | Parses six log formats, JSON exports, SQL snapshots (allow-listed DDL/INSERT only), PDFs and vision-extracted screenshots into one SQLite index. Each observation carries two timestamps: when it was true, which drives state, and when its file was captured, which drives citation. |
| 1 | `stages/intent_classifier.py` | **Claude** (`claude-opus-5-5`, effort `low`) | One structured-output call per distinct request text: 375 calls for 518 cases. Any intent to close or delete the account counts as deletion. |
| 2 | `stages/identity.py` | Code | Exact email match, current or historical, to `user_id`, then every identifier with its validity window. Names are never used for matching. |
| 3 | `stages/retrieval.py` | Code | Two hops: identifier → observation → the resource's full timeline. Log traces join a lookup key to its result. Everything is cut at `audit_at`. |
| 4 | `stages/state_report.py`, `stages/adjudicator/` | **Code or Strands agent** | See §3. Produces the state report: category, observed state, required state, compliance and citable evidence for each record. |
| 5 | `stages/validator.py` | Code | Every report and row is checked against the evidence and the output contract. Error messages are specific enough to give back to the agent. |
| 6 | `stages/steps.py` | Code | Status, operations, step order, numbering and evidence blanking, all by fixed rules derived from the samples. **No model writes steps.** |
| 7 | `stages/output.py`, `pipeline/driver.py` | Code | Flags any record linked to more than one person, writes the CSV, and scores sample runs. |

**Fail-loud gates.** If any request cannot be classified, the run stops after stage 1 (exit code 2, error banner, `cache/run_log.jsonl` entry) instead of quietly treating it as a deletion. `output.csv` is written only when every request is classified, every case validates, and there is exactly one row per request. Otherwise the rows go to `cache/unverified_output.csv`.

**Evidence rules the pipeline enforces.**
- State uses only observations at or before `audit_at`.
- Citations use only files captured at or before `audit_at`.
- Identity links must be exact; lookalike `.jr` emails and shared names are never matched.
- Activity-log entries and workflow statuses are treated as claims.
- A record is never attributed to a person without a provable link.
- Later evidence may prove who a record belongs to, but only if the record also has its own pre-audit evidence.
- Data-catalog ownership claims count only if recorded while that request was being handled.

---

## 3. Deterministic vs agentic: why the split

Most deletion cases can be decided by rules once the evidence is indexed: each record's latest state at `audit_at`, looked up in `deletion_rules.csv`. That path is exact, repeatable and free, so **every case starts with the deterministic report**. The Strands agent is used only where a rule cannot make the call:

| Case gets the agent when it has… | Example |
|---|---|
| An unresolved data category | Support record: shared conversation (redact) or uploaded file (delete)? |
| A record whose nature is unclear | case_12: an "analytics" record matched only by an old email hash is really a contact copy, `SECONDARY_IDENTIFIER`, so it must be deleted |
| Several accounts, a catalog-only ownership claim, or conflicting evidence | case_63: the catalog says `marketing_001` belongs to the subject, but the screenshot shows a different person, so it is excluded |

In the final full run, **468 of 518 cases (90%) were decided deterministically and 50 (10%) went to the agent.** This keeps model cost low, and it means every case that *can* be solved by rules is solved the same way every time.

**How the agent is constrained.**
- **Model and setup:** `claude-opus-5-5` at effort `medium`, through Strands Agents.
- **Tools:** read-only and limited to one case. Every query is cut at `audit_at`: `get_resource_timeline`, `read_evidence`, `get_trace`, `search_identifier`.
- **Answer:** submitted through `submit_state_report`, which runs the stage-5 validator immediately and returns errors for the agent to fix.
- **Limits:** 20 turns and 400k tokens per case.
- **Failure:** without an accepted report, the case keeps its deterministic report and is flagged.
- **Results:** in the full run, 50/50 agent reports were accepted, most on the first submission, in 3-5 turns, at about $0.12 per case.

---

## 4. What the cache is for

The `cache/` directory exists so that **re-running costs nothing for work that has not changed**. It is not a store of pre-computed answers.

| File | Contents | Invalidated when |
|---|---|---|
| `index.db` | Parsed evidence (stage 0) | The dataset, screenshot extraction or index-builder code changes |
| `intent_cache.json` | Claude's intent verdicts, per request text | The prompt version, model or text changes |
| `adjudication_cache.json` | Accepted agent reports | The prompt version, model, effort, case evidence or questions change |
| `judge_cache.json` | Judge verdicts | The prompt version, model, effort or row content changes |
| `media_extractions.json` | Vision transcription of the 11 screenshots | `--force` or a new prompt version |

Every decision is recomputed from the index on every run. Caches only skip *model calls* whose exact inputs were already answered. Example: after a fix round, the full 518-case run took 16 s and re-billed one agent case. Only successful answers are cached, so failures are retried. Per-call token usage goes to `cache/usage_log.jsonl`, and run events go to `cache/run_log.jsonl`.

---

## 5. Accuracy

### 5.1 Labelled samples (32 cases, `dataset/sample_output.csv`)

| Configuration | Status acc. | Step precision | Step recall | **Step F1** | Field acc. | Exact sequence |
|---|---|---|---|---|---|---|
| Deterministic only (`--agent off`) | 1.000 | 0.976 | 0.850 | 0.909 | 0.747 | 0.312 |
| **Deterministic + agent (submitted configuration)** | **1.000** | **0.994** | **0.865** | **0.925** | **0.755** | **0.344** |
| Ceiling under the no-guess linkage policy | 1.000 | 1.000 | 0.865 | 0.928 | n/a | n/a |

- **Steps:** gold has 193 steps. We output 168, and 167 match.
- **Every missed gold step (26) belongs to a record with no provable link to the person** at or before `audit_at`, typically a deleted account or anonymized analytics profile that kept no identifier. Guessing those links by timing was rejected: candidates were rarely unique (4-68 per window), and a guess risks naming another person's record. **On every record we can link, recall is 100%.**
- **The one extra step** is a verification of the person's older, already-deleted account (case_17). It is correctly linked, but gold does not list it.
- **Exact-sequence accuracy** is held down only by those unlinkable records. Every case whose records are all linked matches gold step for step.

### 5.2 Evaluation set (518 cases)

| | Result |
|---|---|
| Status | 154 COMPLETE, 354 INCOMPLETE, 10 NOT_APPLICABLE (the 10 requests that explicitly keep the account) |
| Steps | 2,872 (mean 5.5 per case, max 14) |
| Validation | 518/518 valid; submission gate passed |
| Evidence coverage | 94/94 files indexed, including all 11 screenshots |
| Workflow claims vs truth | 160 of the 254 deletions the workflow reported as COMPLETE still leave data behind |

---

## 6. Evaluation: what it checks and what it does not

`code/evaluation/` runs an **independent single-call judge**: one OpenAI `gpt-6.1-sol` call per case, at reasoning effort `low`. The provider is deliberately different from the pipeline's, to avoid self-agreement. The judge sees:
- the request;
- the evidence the pipeline retrieved, cut at `audit_at`;
- the pipeline's per-record reasoning;
- the output row.

It returns agree/disagree for each step, any missing steps, and a case verdict. It is advisory and never changes `output.csv`.

**The judge mainly catches obvious errors.** It reviews each answer against the evidence the pipeline already gathered. It does not re-search the full dataset, so it cannot find records the pipeline never linked. A more thorough evaluation would give the judge tool access to all data (`index.db`, every evidence file, the activity log) for every case. At the measured cost of an agentic run over this data, that was **estimated at about $60 for the 518 cases**. It was deliberately not run; the single-call design costs about $4.

How reliable is the judge at what it does check? On labelled data:

| Judge model | False alarms on correct sample cases | Planted errors caught (61 mutants) | Cost (93 calls) |
|---|---|---|---|
| GPT-6 Luna | 12/32 | 59/61 (96.7%) | $0.03 |
| **GPT-6.1 Sol (used)** | **0/32** | **61/61 (100%)** | $0.62 |
| GPT-6 Astra | 0/32 | 61/61 (100%) | $3.17 |

Mutants are correct sample rows with one planted error each: flipped status, wrong operation, wrong finding type, dropped resource, another person's resource, or wrong evidence file. Sol matches Astra at about 1/5 of the cost.

**On the 518 evaluation rows, Sol rated all 518 VALID** and agreed with all 2,872 steps. Before the final fixes it had disputed 23 cases. Tracing them led to two real fixes: a citation bug (6 rows) and the screenshot extraction (5 rows). The other 12 disputes were omissions the output rules require. See [`docs/evaluation_analysis.md`](../docs/evaluation_analysis.md).

---

## 7. Cost and token usage

| Run | Model calls | Input / output tokens | Cost |
|---|---|---|---|
| Full 518-case pipeline, clean (no caches) | 424 (366 intent, 58 agent) | 1.51M / 76k | **$7.56** (about $0.015 per request) |
| Single-call judge over 518 rows (Sol) | 518 | 1.27M / 182k | about $4.4 |
| Re-run after fixes, caches kept | 1 agent call | about 18k / 0.7k | $0.08 |

Prices are list prices: `claude-opus-5-5` at $4/$20 per million tokens, `gpt-6.1-sol` at $2/$10. Exact per-call records are in `cache/usage_log.jsonl`.

---

## 8. Code layout

```
code/
├── main.py              entry point (CLI)
├── pipeline/driver.py   runs stages 0-7, enforces the gates, decides where output goes
├── lib/                 config (paths), api (Claude calls: structured output, refusal and error handling, usage logging), logger
├── stages/              evidence_index/ (0), intent_classifier (1), identity (2), retrieval (3),
│                        state_report + adjudicator/ (4), validator (5), steps (6), output (7)
├── tools/               build_index.py, extract_media.py, inspect_case.py
└── evaluation/          judge.py, mutations.py, main.py (independent evaluation)
```

## 9. Known limitations
- **Unlinkable records are left out by design.** This caps sample recall at 0.865.
- **Some records cannot be cited.** Some state is proven only by files captured after `audit_at` (logs and snapshots exported later). Those records cannot be cited, so COMPLETE cases omit their verification, as the output rules require.
- **The judge checks answers, not coverage.** It cannot detect records the pipeline did not link (see §6).
