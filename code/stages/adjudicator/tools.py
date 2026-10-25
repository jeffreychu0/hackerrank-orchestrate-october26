"""Read-only, case-scoped tools for the adjudication agent.

`make_tools(case)` returns Strands tools bound to one case: every query is limited to
observations at or before that case's audit_at, outputs are compact JSON capped in size, and the
submit tool runs the stage-5 validator so the agent can correct its report in the same loop.
"""
from __future__ import annotations

import functools
import json
import os
import re

from strands import tool

from lib import config
from stages.evidence_index.normalize import EMAIL_RE, HASH_RE, USER_RE, norm_email, norm_phone
from stages.retrieval import connect
from stages.validator import validate_report

MAX_CHARS = 6000
MAX_SUBMISSIONS = 4


def _out(obj) -> str:
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS] + '..."[truncated]"'


def _normalise(value: str) -> list[tuple[str, str]]:
    v = value.strip()
    if EMAIL_RE.fullmatch(v):
        return [(norm_email(v), "email")]
    if HASH_RE.fullmatch(v):
        return [(v.lower(), "email_hash")]
    if USER_RE.fullmatch(v.lower()):
        return [(v.lower(), "user_ref")]
    if re.fullmatch(r"\+?[\d ()\-]{7,}", v):
        return [(norm_phone(v), "phone")]
    return [(v.lower(), "email"), (v.lower(), "user_ref")]


class CaseTools:
    """Holds the per-case context; `tools` are the bound Strands tools."""

    def __init__(self, case: dict, bundle: dict, rules: list[dict]):
        self.case, self.bundle, self.rules = case, bundle, rules
        self.audit_at = bundle["audit_at"]
        self.accepted_report: dict | None = None
        self.submissions = 0
        self.last_errors: list[str] = []
        self.tool_errors: list[str] = []
        self.tools = self._build()

    def query(self, sql: str, params: tuple = ()) -> list:
        """Run one read-only query on a fresh connection. Strands executes tools on its own threads, and a
        SQLite connection may only be used by the thread that created it."""
        db = connect(config.INDEX_PATH)
        try:
            return db.execute(sql, params).fetchall()
        finally:
            db.close()

    def close(self) -> None:
        pass

    def guard(self, fn):
        """Wrap a tool body so failures are recorded (and flagged on the case) instead of silently absorbed."""
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                self.tool_errors.append(f"{fn.__name__}: {type(exc).__name__}: {exc}")
                return _out({"error": f"{type(exc).__name__}: {exc}"})
        return wrapper

    def _build(self):
        ctx = self

        @tool
        @ctx.guard
        def get_resource_timeline(resource_id: str) -> str:
            """Every observation of one resource at or before audit_at (oldest first), with the evidence file,
            store (primary/replica/log/readback/...), derived state, and an obs_id you can pass to read_evidence.
            Also returns activity-log claims about the resource up to audit_at.

            Args:
                resource_id: e.g. account_5334, search_9359, support_6477, data_flow_8524.
            """
            obs = ctx.query(
                "SELECT obs_id, file_id, file_captured_at, obs_time, store_role, derived_state, record_type, "
                "container, system_id FROM observations WHERE resource_id=? AND obs_time<=? ORDER BY obs_time, obs_id",
                (resource_id, ctx.audit_at))
            acts = ctx.query(
                "SELECT activity_id, event_time, event_type, reported_result, details FROM activity "
                "WHERE (resource_id=? OR downstream_resource_id=?) AND event_time<=? ORDER BY event_time, seq",
                (resource_id, resource_id, ctx.audit_at))
            return _out({"resource_id": resource_id, "audit_at": ctx.audit_at,
                         "observations": [dict(r, citable=r["file_captured_at"] <= ctx.audit_at) for r in obs],
                         "activity_claims": [dict(r) for r in acts]})

        @tool
        @ctx.guard
        def read_evidence(obs_id: int) -> str:
            """The underlying evidence for one observation: the parsed record fields, and for log/text files the raw
            source lines around it (lines dated after audit_at are hidden).

            Args:
                obs_id: an obs_id from get_resource_timeline.
            """
            rows = ctx.query("SELECT * FROM observations WHERE obs_id=? AND obs_time<=?",
                                 (obs_id, ctx.audit_at))
            row = rows[0] if rows else None
            if not row:
                return _out({"error": f"obs_id {obs_id} not found at or before audit_at"})
            result = {"obs_id": obs_id, "file_id": row["file_id"], "source": row["source_ref"],
                      "resource_id": row["resource_id"], "state": row["derived_state"],
                      "fields": json.loads(row["fields_json"])}
            m = re.match(r"(.+\.(?:log|txt)):(\d+)$", row["source_ref"])
            if m:
                path, line_no = os.path.join(config.DATASET_DIR, *m.group(1).split("/")), int(m.group(2))
                with open(path, encoding="utf-8") as fh:
                    lines = fh.read().splitlines()
                window = []
                for n in range(max(1, line_no - 4), min(len(lines), line_no + 4) + 1):
                    ts = lines[n - 1].split(" ", 1)[0]
                    if not re.match(r"\d{4}-\d\d-\d\dT", ts) or ts <= ctx.audit_at:
                        window.append(f"{n}: {lines[n - 1]}")
                result["source_lines"] = window
            return _out(result)

        @tool
        @ctx.guard
        def get_trace(file_id: str, trace_id: str) -> str:
            """All raw log lines of one trace in one evidence file (dated at or before audit_at). A lookup's
            'started' line carries the key being looked up; the 'completed' line carries the result.

            Args:
                file_id: e.g. evidence_01.
                trace_id: e.g. tr-8001359 or rb-074950-001.
            """
            rows = ctx.query("SELECT file_path FROM evidence_files WHERE file_id=?", (file_id,))
            row = rows[0] if rows else None
            if not row or not row["file_path"].endswith(".log"):
                return _out({"error": f"{file_id} is not a log file"})
            path = os.path.join(config.DATASET_DIR, *row["file_path"].split("/"))
            with open(path, encoding="utf-8") as fh:
                hits = [ln.rstrip("\n") for ln in fh if trace_id in ln and ln.split(" ", 1)[0] <= ctx.audit_at]
            return _out({"file_id": file_id, "trace_id": trace_id, "lines": hits[:40]})

        @tool
        @ctx.guard
        def search_identifier(value: str) -> str:
            """Exact lookup of one identifier (email, phone, email hash, or user_id): who owns it, and which resources
            mention it in evidence at or before audit_at. Use it to check whether a similar-looking identifier
            belongs to this person.

            Args:
                value: the identifier, as written in the evidence.
            """
            out = []
            for ident, kind in _normalise(value):
                owners = [dict(r) for r in ctx.query(
                    "SELECT user_id, is_current, valid_from, valid_to FROM identities WHERE identifier=? AND id_kind=?",
                    (ident, kind))]
                hits = [dict(r) for r in ctx.query(
                    "SELECT DISTINCT o.resource_id, o.file_id, o.obs_time, o.derived_state, oi.link_via "
                    "FROM observation_identifiers oi JOIN observations o USING(obs_id) "
                    "WHERE oi.identifier=? AND oi.id_kind=? AND o.obs_time<=? ORDER BY o.obs_time LIMIT 40",
                    (ident, kind, ctx.audit_at))]
                if owners or hits:
                    out.append({"identifier": ident, "kind": kind, "owners": owners, "evidence": hits})
            return _out({"query": value, "matches": out or "no exact match"})

        @tool
        @ctx.guard
        def submit_state_report(resources: list[dict], excluded: list[dict], summary: str) -> str:
            """Submit the final state report for this case. It is validated immediately; fix any errors and resubmit.

            Args:
                resources: one object per reported resource with keys resource_id, system_id, category,
                    observed_state, required_state, compliant, evidence_id, link_basis, rationale.
                excluded: one object per bundle resource you leave out, with keys resource_id and reason.
                summary: one or two sentences on what you decided and why.
            """
            ctx.submissions += 1
            report = {"case_id": ctx.bundle["case_id"], "intent": "DELETION", "source": "agent",
                      "resources": resources, "excluded": excluded, "summary": summary}
            errors = validate_report(report, ctx.bundle, ctx.rules)
            ctx.last_errors = errors
            if not errors:
                ctx.accepted_report = report
                return "ACCEPTED. Reply with one short sentence and stop."
            if ctx.submissions >= MAX_SUBMISSIONS:
                return "REJECTED (submission limit reached; the draft report will be used). Stop now."
            return "REJECTED. Fix these errors and call submit_state_report again:\n- " + "\n- ".join(errors[:20])

        return [get_resource_timeline, read_evidence, get_trace, search_identifier, submit_state_report]
