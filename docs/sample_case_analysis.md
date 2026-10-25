# Sample Case Analysis: What Makes a Deletion COMPLETE vs INCOMPLETE

Source: `dataset/sample_deletion_requests.csv` + `dataset/sample_output.csv` (32 cases), cross-referenced with the evidence each answer cites. Companion to [dataset_analysis.md](dataset_analysis.md).

## 1. Overview

| Truth \ Reported | COMPLETE | PARTIALLY_COMPLETE | NOT_STARTED | Total |
|---|---|---|---|---|
| **COMPLETE** | 01, 03, 04, 21, 27, 31 | 02 | 25, 26, 28 | **10** |
| **INCOMPLETE** | 05, 07, 10, 11, 12, 14, 15, 18, 22, 24, 30 | 06, 08, 19, 32 | 09, 13, 16, 17, 20, 23, 29 | **22** |
| NOT_APPLICABLE | none | none | none | 0 |

**`reported_status` has no predictive value.** 11 of 17 "COMPLETE" claims are wrong, and 3 of 10 "NOT_STARTED" claims are actually complete. Decide every case from evidence.

Phase shapes: all 10 COMPLETE cases are `VERIFICATION`-only. All 22 INCOMPLETE cases are `FINDING → REMEDIATION → VERIFICATION`.

## 2. The decision rule

A deletion request is **COMPLETE** at `audit_at` if and only if every resource tied to the subject (current or historical identifiers) is in its policy-required end state, according to the latest evidence captured at or before `audit_at`:

| Resource (category) | Required end state | Evidence signal: compliant | Evidence signal: violation |
|---|---|---|---|
| Account profile (`USER_PROFILE`) | ABSENT | `http_status=404`, `rows=0`, `status_code: 404`, `NOT_FOUND`; no row in primary **or any replica** table | `200`, `rows=1`, `FOUND`, row still in a replica (case_05) |
| Search document (`SEARCH_INDEX_ENTRY`) | ABSENT | no hit | `result=hit`, `FOUND`, document in index query |
| User export (`USER_EXPORT`) | ABSENT | not found / unavailable | `download_available: true`, `FOUND` |
| Support attachment, subject-only (`SUPPORT_ATTACHMENT`) | ABSENT | not found | attachment or document present (case_07 PDF, case_13 `SUPPORT_ATTACHMENT FOUND`) |
| Support message/ticket, shared context (`SUPPORT_MESSAGE`) | REDACTED | `requester_name/email/phone = null` (a stable `customer_reference` may remain) | `requester_email=…`, `IDENTIFIABLE`, `message_pii_scan matched_value=…` |
| Analytics profile (`ANALYTICS_PROFILE`) | ANONYMIZED | `user_id`, `customer_reference`, `email`, `phone`, `email_hash` **all null** | any one of them populated (even if `customer_reference` is null) |
| Account tombstone (`ACCOUNT_TOMBSTONE`) | REDACTED | only the stable `user_id` remains | name/email/phone/hash still present (case_32) |
| Security audit log (`SECURITY_AUDIT_LOG`) | PRESENT (retain) | record readable, `retention_until` in the future | never a violation in the samples. Don't delete it. |
| Integration connector (data flow) | DISABLED | `enabled=false`, `last_delivery_status=stopped`, `DISABLED` | `enabled=true`, `ACTIVE`, heartbeat, "remains enabled", connector repopulation |
| Backup archive (`BACKUP_ARCHIVE`) | RETAINED **and** restore BLOCKED | `restore_allowed=false`, `BLOCKED` | `restore_allowed=true`, `ACTIVE`, "can still be restored". The fix is to block restore, never to delete. |

If **any** row is in the violation column, the case is **INCOMPLETE**.

## 3. How the output is built

### 3.1 COMPLETE cases (verification only)
- One `VERIFICATION` step per relevant resource, each proving its compliant end state.
- **Every step cites an evidence id** (a `file_id` captured at or before `audit_at`) that shows that resource's state. Different steps may cite different files (case_04 cites evidence_54 and evidence_56).
- The account `VERIFY_RECORD_ABSENT` step is always present.
- Every other resource of the subject that has a compliant state is included: the retained security record, redacted support, anonymized analytics, disabled flow, blocked backup, redacted tombstone (cases 26–28 list all six).

### 3.2 INCOMPLETE cases (findings, then remediation, then verification)
1. **FINDING**: one step per violation, citing the evidence that shows it.
2. **REMEDIATION**: one step per finding, with `evidence_id` set to `""`.
3. **VERIFICATION**: `evidence_id` is `""` for all of them, **including resources that are already compliant**. The set is:
   - the account `VERIFY_RECORD_ABSENT` (always, even when the account was already gone);
   - the retained security record `VERIFY_RECORD_PRESENT` (whenever the subject has one);
   - one verification for each remediated resource.

   Resources that are already compliant and weren't remediated (other than the account and security record) don't appear in INCOMPLETE verification lists. For example, case_09 lists only the account, security record and backup.

### 3.3 Finding → remediation → verification mapping

| Violation | Finding (state) | Remediation (state) | Verification (state) |
|---|---|---|---|
| Record that must be deleted still exists | `REPORT_REMAINING_RECORD` (PRESENT) | `DELETE_RECORD` (ABSENT) | `VERIFY_RECORD_ABSENT` (ABSENT) |
| Retained record still exposes identifiers (support message, tombstone) | `REPORT_EXPOSED_PII` (PRESENT) | `REDACT_RECORD` (REDACTED) | `VERIFY_RECORD_REDACTED` (REDACTED) |
| Analytics still attributable | `REPORT_EXPOSED_PII` (PRESENT) | `ANONYMIZE_RECORD` (ANONYMIZED) | `VERIFY_RECORD_ANONYMIZED` (ANONYMIZED) |
| Connector still enabled | `REPORT_ACTIVE_DATA_FLOW` (ACTIVE) | `DISABLE_DATA_FLOW` (DISABLED) | `VERIFY_DATA_FLOW_DISABLED` (DISABLED) |
| Backup still restorable | `REPORT_RESTORE_RISK` (ACTIVE) | `BLOCK_RESTORE` (BLOCKED) | `VERIFY_RESTORE_BLOCKED` (BLOCKED) |
| (retained, compliant) security record | none | none | `VERIFY_RECORD_PRESENT` (PRESENT) |

`system_id` is the logical service (`account_service`, `search_service`, `support_platform`, `analytics_platform`, `export_service`, `integration_service`, `backup_service`, `security_service`, `identity_service`). It is never a replica or table name: the replica ghost in case_05 is reported as `account_service / account_005`.

### 3.4 Step ordering (holds for all 32 samples)
- **Findings**: `REPORT_REMAINING_RECORD` → `REPORT_EXPOSED_PII` → `REPORT_ACTIVE_DATA_FLOW` / `REPORT_RESTORE_RISK`. Within an operation, sort by `system_id` alphabetically (account < analytics < export < search < support).
- **Remediation**: `DISABLE_DATA_FLOW` / `BLOCK_RESTORE` **first** (stop recreation), then `DELETE_RECORD` → `REDACT_RECORD` → `ANONYMIZE_RECORD`. Within an operation, sort by `system_id`. Note that REDACT comes before ANONYMIZE even though analytics sorts before support (cases 11, 18, 29, 30).
- **Verification**: `VERIFY_RECORD_ABSENT` (account_service first, then the others by `system_id`) → `VERIFY_RECORD_PRESENT` → `VERIFY_RECORD_REDACTED` → `VERIFY_RECORD_ANONYMIZED` → `VERIFY_DATA_FLOW_DISABLED` → `VERIFY_RESTORE_BLOCKED`.

## 4. Edge-case behaviours shown by specific samples

| Behaviour | Sample | What it teaches |
|---|---|---|
| Replica drift | case_05 | Account deleted from primary but still in `customer_profiles_failover_replica`: INCOMPLETE. The same evidence file proves ABSENT for account_001/002/004 (absent from both tables), so those cases are COMPLETE. |
| Historical email as the request email | case_10, 29, 30 | Resolve the old email to a `user_id`, then search every identifier. Violations were found under the old address (`camille.bernard86@…`, `nadia.ionescu@…`). |
| Old identifier found only through its hash | case_12 | `analytics_9914` was found by `correlation_key=<old email_hash>` (contact record with `email_opt_in=true`). It was treated as `REPORT_REMAINING_RECORD` → `DELETE_RECORD`, **not** anonymize. A record keyed only by a stale identifier counts as a secondary-identifier copy. |
| State changes after audit_at | case_27 | The 2026-09-17 snapshot shows account_3866 **ACTIVE again**, but `audit_at` is 09-15, so the case is COMPLETE using the 09-15 read-back (404). Later evidence is ignored. |
| Stale "still exists" report | case_26 | Older claims of existence are overridden by the latest pre-audit evidence (404): COMPLETE. |
| Evidence at exactly audit_at | case_11, 13, 14 | evidence_03 `captured_at` = `audit_at` = 2026-10-01T12:00:00Z and is cited, so the cutoff is **inclusive** (≤). |
| Decoy lookalike | case_28, case_06 | `smartinez6461.jr@…` and `jonas.berglund@…` are ignored. Only exact identifier matches count. |
| Tombstone compliant vs exposed | case_31 vs 32 | A tombstone that keeps only the stable `user_id` is COMPLETE (`VERIFY_RECORD_REDACTED`). A tombstone that still has PII is `REPORT_EXPOSED_PII` → `REDACT_RECORD`. |
| Backup is never deleted | case_09, 14, 17 | A restorable backup leads to `BLOCK_RESTORE`. The archive itself is retained. |
| Subject-only attachment vs shared conversation | case_07/13 vs 11/18/19 | An attachment is deleted. A conversation with shared context is redacted. |
| `support_002` "deleted" SUCCESS claim | case_10 | Activity says SUCCESS, but the replica read-back shows `marker_found=false` with the record still returned: INCOMPLETE. |

## 5. How the samples link evidence to the subject

1. **Direct identifier match** on any current or historical email (case-insensitive), phone, `email_hash`, or `user_id` / `customer_reference` / `subject_reference`.
2. **Trace correlation in logs.** The `*_started` line carries `lookup_key=` / `subject_key=` / `subject_ref=`, and the `*_completed` line with the **same `trace_id`** carries the `resource_id` and the result (`rows=0 http_status=404`, `enabled=true`, `integrity_check=passed`). Matching line by line misses these. Group by `trace_id` first.
3. **Lookup-record formats** (`system | resource | type | RESULT | subject`): the subject column gives the link directly.
4. **Catalog claims** (`LINK_RESOURCE_IDENTITY`). Use them only as a hint and confirm against evidence content (see the `marketing_001` conflict in dataset_analysis.md).

## 6. Gaps and open questions for the design

1. **Some gold resources aren't linkable from the evidence.** In about 13 samples (for example case_21: `account_6440`, `analytics_5335`; cases 25–28: their account and analytics ids), the read-back shows the record already deleted or anonymized, so it carries **no identifier**. No other participant file maps it to the subject. The deletion worker processes many accounts in one batch, so `system_activity` timing alone is ambiguous. We need a strategy for these. Options:
   - temporal clustering of `DELETE_RECORD`/`ANONYMIZE_RECORD` activity after `requested_at`, combined with position in the same read-back export;
   - accepting that the account verification can't always be grounded.
2. **There are no NOT_APPLICABLE samples.** The rule (`[]` for non-deletion requests) comes only from the spec and the 10 `CLASSIFY_REQUEST` activity rows.
3. **Ordering edge cases aren't covered.** No sample has both a data flow and a backup finding, or two remediations of the same type in a dependency chain. The order of DISABLE vs BLOCK, and "downstream before source", are not confirmed by any sample.
4. **When to DELETE an analytics record instead of ANONYMIZE it** rests on a single example (case_12). The working hypothesis is that a record matched only through a stale identifier and acting as a contact record gets deleted.
5. **Hand-built cases 01–08 and 10 have no security-record verification**, while the generated cases do. This suggests "verify PRESENT only when a retained security record exists in evidence", not "always".
