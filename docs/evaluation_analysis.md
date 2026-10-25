# Evaluation Analysis: Fix Round, F1 on the Samples, Judge Models

Run date: 2026-10-25. This follows [e2e_results.md](e2e_results.md), the clean baseline run. All caches were kept, so only work whose inputs changed was recomputed. Artefacts are in `cache/e2e/`: `output_before_fixes.csv`, `output_after_fixes.csv`, `*_fixed.*`, and the judge summaries per model.

## 1. Changes made in this round

| # | Change | Where | Why |
|---|---|---|---|
| 1 | **Citation state classes.** PRESENT and IDENTIFIABLE now count as one "still holds data" class when choosing citable evidence. | `stages/retrieval.py` | **Bug G2.** A citable earlier observation (IDENTIFIABLE) was discarded because a later, uncitable one said PRESENT, so real violations were excluded. |
| 2 | **Vision extraction of the 11 screenshots.** A one-time pass with `claude-opus-5-5` (effort `medium`, strict schema) into `cache/media_extractions.json`, which the index builder ingests. | `tools/extract_media.py`, `evidence_index/parsers.py` | **Gap G3.** Five cases' only linked resource was known solely from a screenshot. |
| 3 | **Origin tie-break for aliases.** When an alias names several resources, keep the one whose system matches the evidence file's origin, if exactly one does. | `evidence_index/builder.py` | `delivery_confirmation_19.pdf` names both `object_19` and `support_3802`. The object-storage screenshot's origin is `object_storage`, so `object_19` is chosen. |
| 4 | **Catalog-claim window.** An unverified catalog ownership claim counts only if recorded between `requested_at` and `audit_at`. | `stages/retrieval.py` | Every one of the 12 catalog claims falls inside exactly one case's request window. The out-of-window ("stale") matches are sample cases where gold excludes the resource (`object_19` for case_07, `tombstone_17` for case_17). |
| 5 | **Judge v3.** The judge is told the citation rule. Added mutation testing and multi-model runs. | `evaluation/judge.py`, `evaluation/mutations.py`, `evaluation/main.py` | **G1 disputes.** The judge flagged omissions that the output rules require. |

**Vision extraction review.** All 11 screens were checked by hand against the images: 37 records, every id, identifier and timestamp transcribed correctly. The model left `restore_allowed` blank on the backup screen rather than guessing it. It also omitted the analytics screen's record time, which is later than the file's capture time. One small gap: `data_flow_9846` was not marked `enabled`, but it still derives as an active flow from its identifiable selector. Extraction cost: 11 calls, 48k input / 12k output tokens, $0.44.

## 2. Effect on the 518-row output

| | Before fixes | **After fixes** |
|---|---|---|
| COMPLETE / INCOMPLETE / NOT_APPLICABLE | 162 / 346 / 10 | **154 / 354 / 10** |
| Steps | 2,821 | **2,872** |
| COMPLETE rows with no steps | 9 | **1** (case_63, deliberate) |
| Files indexed | 83 / 94 | **94 / 94** |

**17 rows changed, 51 steps added:**

| Cases | Change |
|---|---|
| case_131, 179, 227, 275, 431, 527 | Citation fix: each now reports its still-present support attachment, citing the earlier evidence file. case_179 changes from COMPLETE to INCOMPLETE. |
| case_35, 39, 50, 52, 55 | Screenshots supply evidence: live search entry, active connector, identifiable analytics profile, downloadable export, restorable backup. All five change from COMPLETE to INCOMPLETE. |
| case_33, 34, 53 | Screenshots add a finding: Ingrid's export; Haruka's account still shown present a day before the audit; Kofi's support conversation needing redaction. case_53 changes from COMPLETE to INCOMPLETE. |
| case_45, 46 | Screenshots add verifications to a COMPLETE case: Oliver's six retained security records; Leonie's redacted deleted-account reference. |
| case_61 | Alias tie-break plus a claim inside the request window: Priya's stored attachment `object_19` is reported (finding cites `evidence_46`). COMPLETE changes to INCOMPLETE. |

**case_63 stays COMPLETE with no steps, deliberately.** The catalog claims `marketing_001` belongs to Kwame Mensah, but the screenshot shows Soo-jin Park (`sj.park@naver.com`, a Korean phone number), none of which match the subject. The agent excluded it as an uncorroborated, contradicted claim.

## 3. F1 on the known dataset (32 labelled samples)

| Configuration | Status acc. | Precision | Recall | **F1** | Field acc. | Exact sequence |
|---|---|---|---|---|---|---|
| Rules only (`--agent off`) | 1.000 | 0.976 | 0.850 | **0.909** | 0.747 | 0.312 |
| Rules + Strands agent (final) | 1.000 | 0.994 | 0.865 | **0.925** | 0.755 | 0.344 |
| *Ceiling under the no-guess linkage policy* | 1.000 | 1.000 | 0.865 | *0.928* | n/a | n/a |

- **We are at the ceiling.** Gold has 193 steps. We output 168, of which 167 match. All 26 missed gold steps belong to resources with no provable link to the subject at or before `audit_at`: deleted accounts and anonymized analytics that keep no identifier, plus two audit records absent from every participant file. **On resources we can link, recall is 100%.** The only extra step is case_17's older account `account_6523`, which is correctly linked but not listed in gold.
- **What the agent adds:** it fixes the cases rules cannot decide, namely case_12 (old-hash contact record → `SECONDARY_IDENTIFIER`, delete) and case_07 (uploaded PDF → attachment). Agent cost on the samples is about $0.12 per flagged case.
- **The fixes did not move sample F1,** because no sample had a G2 or G3 defect. Fix 3 briefly *lowered* precision to 0.977 by reporting `object_19` for case_07 from a claim made six months before that request. Fix 4 removed that, confirming the window rule against gold.

## 4. Judge analysis across models

### 4.1 Reliability on labelled data (32 clean samples + 61 planted errors)
Mutants are copies of correct sample rows with one planted error each. Error types: flip the status, swap an operation, swap the finding type, drop a resource's steps, substitute another person's resource, cite the wrong evidence file.

| Judge (reasoning effort `low`) | False alarms on clean cases | Status vs gold | Planted errors caught | Cost (93 calls) |
|---|---|---|---|---|
| GPT-6 Luna | **12/32** cases wrongly INVALID (8 bad step disputes, 5 wrong missing-step claims) | 28/32 | 59/61 (96.7%); misses 2/5 finding-type swaps | $0.03 |
| **GPT-6.1 Sol** | **0/32** | 32/32 | **61/61 (100%)** | $0.62 |
| GPT-6 Astra | 0/32 | 32/32 | 61/61 (100%) | $3.17 |

Catch rate by error type was 100% for Sol and Astra on every type: status 16/16, operation 14/14, evidence 10/10, foreign resource 10/10, dropped resource 6/6, finding type 5/5. **Sol matches Astra at about 1/5 of the cost; Luna is cheap but noisy.**

Caveats:
- Mutations change steps only; the decision basis is left intact, so many mutants are internally inconsistent and easier to spot than a real reasoning error.
- None of the judges can find resources the pipeline did not link (0/26), because they only see linked evidence.

### 4.2 Verdicts on the 518 evaluation rows
| Judge | Before fixes (v2 prompt) | **After fixes (v3 prompt)** |
|---|---|---|
| GPT-6.1 Sol | 495 VALID, 18 INVALID, 5 UNSURE; 33 missing-step claims | **518 VALID; agrees with all 2,872 steps; 0 missing-step claims** |
| GPT-6 Luna | n/a | 296 VALID, 217 INVALID, 5 UNSURE; 206 step disputes, 118 missing-step claims |

How the 23 pre-fix Sol disputes were resolved:
- **G2 (6 cases):** fixed by change 1.
- **G3 (5 cases):** fixed by change 2.
- **G1 (12 cases):** the citation rule now explains them. The pipeline was already correct.

Luna's 217 disputes are noise, consistent with its 37.5% false-alarm rate on clean samples:
- 158 of its 206 step disputes question retained-record verifications that the output contract requires;
- 30 demand evidence on INCOMPLETE verifications, which the contract forbids;
- spot checks show it rejecting alias-resolved links and confusing finding types.

**Recommendation:** use GPT-6.1 Sol as the evaluation judge. Its zero false alarms and full catch rate on planted errors make its 518/518 VALID verdict meaningful. Astra adds cost without adding accuracy here.

## 5. Cost of this round
| Model | Stage | Calls | Input (cached) | Output | Cost |
|---|---|---|---|---|---|
| claude-opus-5-5 | media extraction | 11 | 48,346 | 12,126 | $0.44 |
| claude-opus-5-5 | agent (changed bundles only) | 6 runs | about 148k | about 5k | $0.70 |
| gpt-6.1-sol | judge (samples, mutants, full) | 613 | 1.61M (667k cached) | 210k | $4.04 |
| gpt-6-luna | judge (samples, mutants, full) | 613 | 1.61M (668k cached) | 262k | $0.23 |
| gpt-6-astra | judge (samples, mutants) | 93 | 241k (101k cached) | 33k | $3.17 |
| **Total** | | | | | **≈ $8.6** |

Stage-1 intents and the other 49 agent reports came from cache at no cost.

## 6. Remaining known limitations
1. **Unlinkable resources** (26 sample steps). By design they are not guessed, so recall is 0.865 against a 0.928 F1 ceiling.
2. **case_17's extra old account.** Correctly linked; whether old, already-deleted accounts should be verified is a policy question gold answers "no" to.
3. **Uncitable-only evidence (G1).** For example, case_64's account is shown absent only by a log captured after `audit_at`. Its verification is omitted in COMPLETE cases, as the output rules require.
4. **Judge blind spots.** It sees only linked evidence, so it cannot catch omissions of unlinked resources. The mutation test uses synthetic step-only errors.
