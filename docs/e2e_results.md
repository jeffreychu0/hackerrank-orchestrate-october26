# End-to-End Run: Results, Evaluation and Analysis

> **Status update (same day):** fixes 1-3 below have been applied, together with an alias tie-break and a catalog-claim window rule. `output.csv` now reflects them: 17 rows changed, and Sol judges 518/518 rows VALID. See [evaluation_analysis.md](evaluation_analysis.md). This document records the clean baseline run.

Run date: 2026-10-25, from a clean state. The previous `cache/` was moved to `cache_backups/cache_20261025_060150/`, so the index, every intent verdict, every agent report and every judge verdict were recomputed live. Related docs: [pipeline.md](pipeline.md), [sample_case_analysis.md](sample_case_analysis.md), [dataset_analysis.md](dataset_analysis.md).

## 1. What was run

| Phase | Command | Wall time | Exit | Output |
|---|---|---|---|---|
| Stage 0 index | `python code/tools/build_index.py` | 0.9 s | 0 | `cache/index.db`: 83/94 files indexed (11 images await vision extraction), 2,903 observations |
| Samples pipeline | `python code/main.py --samples --verbose` | 35.6 s | 0 | `cache/sample_predictions.csv`, `cache/sample_evaluation.json` |
| Samples judge | `python code/evaluation/main.py` | about 1 min | 0 | `cache/e2e/samples_judge_summary.json` |
| **Full pipeline** | `python code/main.py --verbose` | 322.7 s | 0 | **`output.csv` (518 rows), passed the submission gate** |
| Full judge | `python code/evaluation/main.py` | about 5 min | 0 | `cache/e2e/full_judge_summary.json` |

Models: `claude-opus-5-5` for stage 1 (intent, effort `low`) and stage 4 (Strands agent, effort `medium`); `gpt-6.1-sol` for the independent judge (reasoning effort `low`). Packages: `anthropic==0.125.0`, `strands-agents[anthropic]==1.62.0`, `openai==3.37.0`, `pypdf==5.1.0`. Every per-run artefact (pipeline logs, per-case traces, judge verdicts) is kept in `cache/e2e/`.

## 2. Accuracy on the 32 public samples (the only labelled cases)

| Metric | Rules only (earlier run) | **This run (rules + agent)** |
|---|---|---|
| Case status accuracy | 1.000 | **1.000** (32/32) |
| Step precision | 0.976 | **0.994** (167/168 predicted steps are in gold) |
| Step recall | 0.850 | **0.865** |
| Step F1 | 0.909 | **0.925** |
| Field accuracy (position-aligned) | 0.747 | **0.755** |
| Exact sequence accuracy | 0.312 | **0.344** (11/32) |

- **Recomputed live:** all 32 intents were recomputed (32/32 DELETION, matching gold) and all 5 flagged cases were re-adjudicated by the agent. The scores match the earlier cached run exactly.
- **Remaining wrong step:** the only predicted step not in gold is case_17's verification of the person's older account `account_6523`. It is correctly linked, but gold does not list it.
- **Missed steps:** almost all of them are resources that carry no subject identifier in any evidence at or before `audit_at`, mostly already-deleted accounts. By design they are not guessed ([pipeline.md §8](pipeline.md)). Two of them (`audit_record_6645`, `audit_record_5569`) appear in no participant file at all.
- **Exact-sequence accuracy is held down** by those omitted accounts. Every case where all resources are linked matches gold step-for-step.

## 3. Full evaluation set (518 cases)

### 3.1 Stage results
| Stage | Result |
|---|---|
| 1 Intent | 508 DELETION, **10 NOT_DELETION** (case_533–542), 0 unclassified, 0 flags, every call served by `claude-opus-5-5` (no refusal fallbacks) |
| 2 Identity | 408 current + 100 historical request emails, 0 unresolved, 1 repeat subject (`user_18`: case_33 and case_52) |
| 3 Retrieval | 270 cases have no linkable account record (`NO_ACCOUNT_RESOURCE_LINKED`, the known gap) |
| 4 Adjudication | 58 flagged cases went to the agent, **58/58 accepted** (52 on the first submission, 6 on the second), 3-5 turns each, no tool errors. The other 460 use the deterministic report |
| 5 Validation | 518/518 reports and rows valid |
| 6 Status | **COMPLETE 162, INCOMPLETE 346, NOT_APPLICABLE 10**; 2,821 steps (mean 5.45, max 14 per case) |
| 7 Output | Submission gate passed: 518 unique rows, sorted, valid JSON |

**The 10 non-deletion requests:**

| Case | Type |
|---|---|
| case_533, case_537, case_541 | Correction |
| case_534, case_538 | Marketing opt-out |
| case_535 | Data export |
| case_536 | Account support |
| case_539, case_540, case_542 | Data access |

All 10 explicitly ask to keep the account. This matches the 10 `CLASSIFY_REQUEST` entries in the activity log and the dataset analysis.

**Operations produced:**

| Phase | Operations |
|---|---|
| Findings | REPORT_REMAINING_RECORD 366, REPORT_EXPOSED_PII 204, REPORT_ACTIVE_DATA_FLOW 112, REPORT_RESTORE_RISK 25 |
| Remediation | DELETE 366, REDACT 132, ANONYMIZE 72, DISABLE_DATA_FLOW 112, BLOCK_RESTORE 25 |
| Verification | ABSENT 539, PRESENT 424, REDACTED 169, ANONYMIZED 72, DISABLED 145, BLOCKED 58 |

### 3.2 The workflow's own status claims are unreliable
| Reported status | → COMPLETE | → INCOMPLETE | → NOT_APPLICABLE |
|---|---|---|---|
| COMPLETE (254) | 94 | **160** | 0 |
| PARTIALLY_COMPLETE (152) | 37 | 115 | 0 |
| NOT_STARTED (112) | **31** | 71 | 10 |

63% of the deletions the workflow reported as COMPLETE still leave data behind. 31 reported as NOT_STARTED are in fact complete. This confirms the dataset's premise that `reported_status` is a claim, not evidence.

### 3.3 What the agent changed
In the 58 adjudicated cases the agent settled 42 unresolved support records as `SUPPORT_MESSAGE` (redact). It excluded 18 resources with reasons:
- 7 `LINKED_ONLY_AFTER_AUDIT_WITHOUT_PRE_AUDIT_EVIDENCE`
- 4 catalog claims without evidence
- 7 `VIOLATION_WITHOUT_CITABLE_EVIDENCE` (see §4.2, group G2)

## 4. Independent evaluation: OpenAI judge

### 4.1 Calibration on the samples
| Check | Result |
|---|---|
| Judge's expected status = gold | 32/32 |
| Correct steps the judge agreed with | 167/167 |
| Wrong steps the judge flagged | 0/1 (missed case_17's extra account) |
| Gold steps we omitted that it found | 0/26 (it only sees linked resources, by design) |

On the samples, the judge confirms that our output is well supported. With only one wrong step there, it gives no measure of how sensitive the judge is to errors.

### 4.2 Verdicts on the 518 evaluation rows
**495 VALID, 18 INVALID, 5 UNSURE.** It agreed with all 2,821 individual steps and with our status in 98.6% of cases. Its objections are all about **missing** steps (33 claims). Each disputed case was traced to its evidence:

| Group | Cases | What happened | Verdict |
|---|---|---|---|
| **G1** | 12: case_64, 69, 70, 126, 213, 215, 218, 219, 221, 335, 336, 339 | The resource's only evidence is an observation dated before `audit_at` inside a file *captured after* `audit_at`. Logs and snapshot bundles are exported days after the events they contain. Example: case_64's `account_565` is shown ABSENT by a log line at 08:56, in `evidence_06`, captured two days after the 09:00 audit. The output rules forbid citing evidence captured after `audit_at`, so a COMPLETE case cannot include that verification. For case_126 the account was *present*, but the only proof is uncitable. | **Pipeline behaves as specified.** The judge was not told the citation rule. |
| **G2** | 5 (+1 overlap): case_131, 227, 275, 431, 527, 179 | **Bug.** A citable observation showed the record IDENTIFIABLE, for example `support_2315` in `evidence_87` at 09:42, before the 10:00 audit. A later uncitable observation said PRESENT. Citation required the *exact* state label, so the citable evidence was discarded and the agent had to exclude a real violation. PRESENT and IDENTIFIABLE both mean the record still holds data. | **Judge is right.** These rows miss a finding, its remediation and its verification. |
| **G3** | 5: case_35, 39, 50, 52, 55 | The only linked resource (`search_428`, `data_flow_1121`, `analytics_7438`, `export_1260`, `backup_9287`) is known from a catalog claim plus a screenshot we have not extracted yet. No verifiable evidence exists, so the case is COMPLETE with no steps. | **Judge is right.** COMPLETE is unsupported until the 11 images are indexed. For example, the search-console screenshot shows `search_428` still indexed for case_35's subject, which would make it INCOMPLETE. |

Net effect: about **11 of 518 rows (2%)** have a known defect: the 6 G2 cases and the 5 G3 cases. The 12 G1 disputes are not defects under the output rules.

## 5. Token usage and cost (this end-to-end run)

| Provider / model | Stage | Calls | Input tokens | Output tokens | Est. cost |
|---|---|---|---|---|---|
| Anthropic `claude-opus-5-5` | 1 intent | 398 | 304,129 | 23,803 | $1.69 |
| Anthropic `claude-opus-5-5` | 4 agent | 63 runs (about 4 turns each) | 1,358,228 | 59,367 | $6.62 |
| **Anthropic subtotal** | | 461 | 1,662,357 | 83,170 | **$8.31** |
| OpenAI `gpt-6.1-sol` | judge | 550 | 1,345,476 (14,682 cached) | 192,555 | **$4.59** |
| **Total** | | 1,011 | 3,007,833 | 275,725 | **≈ $12.90** |

Breakdown for the full 518-case pipeline run alone: 424 model calls, 1.51M input and 76k output tokens, about **$7.56**. That is about $0.015 per request, or about $0.023 per request including the judge. Prices are list prices: Opus 5.5 at $4/$20 per million tokens, GPT-6.1 Sol at $2/$10 (cached input $0.10). Per-call records are in `cache/usage_log.jsonl`.

## 6. Recommended fixes, in priority order

1. **G2 citation bug (code fix, about 6 rows).** In `stages/retrieval.py`, treat PRESENT and IDENTIFIABLE as one "still holds data" class when choosing citable evidence. Then a citable earlier observation supports the finding.
2. **Image extraction (about 5 rows plus agent context).** Run a one-time vision pass over the 11 screenshots into `cache/media_extractions.json`, which the index builder already ingests. That gives evidence for the G3 resources and for case_63's conflicting `marketing_001`.
3. **Judge prompt (no output change).** Tell the judge the citation rule, so G1 omissions are not reported as defects. Then add the proposed mutation test to measure the judge's ability to catch errors.
4. **COMPLETE rows with no steps (9 rows).** These are the G3 cases plus 4 others. Decide whether "nothing verifiable" should stay COMPLETE with `[]` or be reported differently. Fix 2 removes most of them.
5. **Known, accepted gap.** Accounts with no surviving identifier stay out of the output by design. This costs recall on the samples (0.865), but avoids naming other people's records.

Fixes 1 and 2 change only the affected cases. With the caches in place, a re-run re-bills only stage-4 cases whose bundles change, plus the judge for changed rows: an estimated $1-2.

## 7. Reproducing this run
```
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt     # .venv/bin/ on macOS/Linux
cp .env.example .env    # add ANTHROPIC_API_KEY and OPENAI_API_KEY
python code/tools/build_index.py
python code/main.py --samples && python code/evaluation/main.py          # sample accuracy + judge calibration
python code/main.py && python code/evaluation/main.py                    # output.csv + judge review
```
