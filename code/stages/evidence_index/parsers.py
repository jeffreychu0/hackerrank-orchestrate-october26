"""Parsers: one per evidence family. Each turns a file into observations on the Sink.

Formats handled (surveyed from dataset/evidence):
  logs  - six line layouts (key=value, [bracketed], pipe-delimited, JSON, syslog, cloud requestId/message)
  json  - snapshot bundles, service read-back exports, lookup-record exports, single-collection captures
  sql   - inventory snapshots (typed tables + snapshot_scope) and replica snapshots (record_json blobs)
  txt   - shift handover lookup lines
  pdf   - business documents (text via pypdf, falling back to pdftotext)
  image - facts from a cached vision extraction (cache/media_extractions.json), if present
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
from collections import defaultdict

from .model import EvidenceFile, Sink
from .normalize import (
    LOOKUP_KEY_FIELDS, RESOURCE_PREFIX_SYSTEM, RESOURCE_RE, as_bool, extract_identifiers, find_aliases,
    find_resources, flatten, is_null, system_for_resource,
)

KNOWN_SYSTEMS = set(RESOURCE_PREFIX_SYSTEM.values())
NOISE_LOG_FIELDS = {"instance", "pod", "region", "attempt", "duration_ms", "wait_ms", "took_ms", "shard",
                    "latency_ms", "severity", "logger", "level", "message"}
ALIAS_FIELDS = {"ticket_id": "ticket", "order_reference": "order", "file_name": "file", "file": "file"}


def _alias_fields(sink: Sink, ev: EvidenceFile, resource_id: str | None, fields: dict) -> None:
    for key, kind in ALIAS_FIELDS.items():
        value = fields.get(key)
        if resource_id and not is_null(value):
            sink.alias(str(value), kind, resource_id, ev.file_id)


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------
KV_RE = re.compile(r'(\w+)=(?:"((?:[^"\\]|\\.)*)"|(\S+))')


def _kv(text: str) -> dict:
    return {k: (q.replace('\\"', '"') if q or not bare else bare) for k, q, bare in KV_RE.findall(text)}


def parse_log_line(line: str) -> tuple[str, dict] | None:
    parts = line.split(" ", 1)
    if len(parts) != 2:
        return None
    ts, rest = parts[0], parts[1].strip()
    if rest.startswith("{"):
        d = json.loads(rest)
        d.setdefault("service", d.get("logger"))
    elif rest.startswith("["):
        m = re.match(r"\[(\w+)\] \[([\w.-]+)\] \[([^\]]+)\] ([\w.-]+): ?(.*)", rest)
        if not m:
            return None
        d = {"level": m[1], "service": m[2], "trace_id": m[3], "event": m[4], **_kv(m[5])}
    elif rest.startswith("<"):
        m = re.match(r"<\d+>\d+ (\S+) (\S+) (\S+) - - (.*)", rest)
        if not m:
            return None
        d = {"service": m[2], "trace_id": m[3], **_kv(m[4])}
    elif rest.startswith("|") or re.match(r"^\w+ \| ", rest):
        cols = [c.strip() for c in rest.strip("|").split("|")]
        if len(cols) < 4:
            return None
        d = {"level": cols[0], "service": cols[1], "trace_id": cols[2], "event": cols[3],
             **_kv(" ".join(cols[4:]))}
    elif rest.startswith("requestId="):
        top = _kv(rest)
        d = {"trace_id": top.get("requestId"), "level": top.get("level"), "service": top.get("function"),
             **_kv(top.get("message", ""))}
    else:
        d = _kv(rest)
        if not d:
            return None
    return ts, d


def _log_state(event: str, d: dict) -> tuple[str | None, str | None, str | None]:
    """(presence, explicit_result, state_override) from the signals on one log event."""
    if event == "record_lookup" and d.get("result"):
        return None, d["result"], None
    if d.get("detector") and d.get("matched_value"):
        return "PRESENT", None, "IDENTIFIABLE"
    if "decision" in d:
        return "PRESENT", None, "BLOCKED" if d["decision"].lower() == "deny" else "ACTIVE"
    if "eligible" in d or "deletion_block" in d:
        restorable = as_bool(d.get("eligible")) and not as_bool(d.get("deletion_block"))
        return "PRESENT", None, "ACTIVE" if restorable else "BLOCKED"
    if "direct_identifiers" in d:
        n = int(d["direct_identifiers"]) if str(d["direct_identifiers"]).isdigit() else None
        if n is not None:
            return "PRESENT", None, "IDENTIFIABLE" if n > 0 else "ANONYMIZED"
    if "marker_found" in d:
        return ("ABSENT" if as_bool(d["marker_found"]) else "PRESENT"), None, None
    if "available" in d:
        return ("PRESENT" if as_bool(d["available"]) else "ABSENT"), None, None
    if "total_hits" in d and str(d["total_hits"]).isdigit():
        return ("PRESENT" if int(d["total_hits"]) > 0 else "ABSENT"), None, None
    status = str(d.get("http_status", ""))
    if status.startswith("5"):
        return "UNKNOWN", None, None
    if "rows" in d and str(d["rows"]).isdigit():
        return ("PRESENT" if int(d["rows"]) > 0 else "ABSENT"), None, None
    if status == "404":
        return "ABSENT", None, None
    if status == "200":
        return "PRESENT", None, None
    if event == "index_query_completed" and d.get("result") in ("hit", "miss"):
        return ("PRESENT" if d["result"] == "hit" else "ABSENT"), None, None
    if d.get("integrity_check") == "passed":
        return "PRESENT", None, None
    return "PRESENT", None, None


def parse_log(ev: EvidenceFile, sink: Sink) -> None:
    trace_ids: dict[str, set] = defaultdict(set)
    trace_obs: dict[str, list] = defaultdict(list)
    with open(ev.abs_path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.rstrip("\n")
            if not line.strip():
                continue
            try:
                parsed = parse_log_line(line)
            except (ValueError, json.JSONDecodeError):
                parsed = None
            if parsed is None:
                sink.warn(f"{ev.file_id}: unparsed log line {lineno}")
                continue
            ts, d = parsed
            trace = d.get("trace_id")
            event = d.get("event") or ""
            service = d.get("service")
            for key in LOOKUP_KEY_FIELDS | {"correlation_key", "matched_value"}:
                if key in d and trace:
                    for ident, kind in extract_identifiers(d[key], key):
                        trace_ids[trace].add((ident, kind, key))
            rid = d.get("resource_id")
            if not rid or not RESOURCE_RE.fullmatch(str(rid)):
                continue
            if event.endswith("_started"):
                # A lookup that has only started says nothing about state; its keys already fed the trace.
                continue
            store_role = "log"
            if d.get("system"):
                system = d["system"]
            elif service in KNOWN_SYSTEMS:
                system = service
            elif service == "support_replica":
                system, store_role = "support_platform", "support_replica"
            else:
                system = service or system_for_resource(rid)
            fields = {k: v for k, v in d.items()
                      if k not in NOISE_LOG_FIELDS and k not in ("trace_id", "resource_id", "service")}
            presence, explicit, override = _log_state(event, d)
            obs = sink.observe(
                ev, resource_id=rid, fields=fields, obs_time=ts, obs_time_source="line", store_role=store_role,
                container=event or None, system_id=system, explicit_type=d.get("record_type"), event=event,
                presence=presence, explicit_result=explicit, trace_id=trace, state=override,
                source_ref=f"{ev.path}:{lineno}",
            )
            _alias_fields(sink, ev, rid, fields)
            if trace:
                trace_obs[trace].append(obs)
    for trace, observations in trace_obs.items():
        for obs in observations:
            direct = {(i, k) for i, k, _f, _v in obs.identifiers}
            obs.add_identifiers([t for t in trace_ids.get(trace, ()) if (t[0], t[1]) not in direct], "trace")


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------
def parse_json(ev: EvidenceFile, sink: Sink) -> None:
    with open(ev.abs_path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if "snapshots" in doc:
        _json_snapshots(ev, sink, doc)
    elif "service_exports" in doc:
        _json_service_exports(ev, sink, doc)
    elif "origin" in doc and "records" in doc:
        _json_lookup_records(ev, sink, doc)
    elif "collection" in doc and "records" in doc:
        _json_collection(ev, sink, doc)
    else:
        sink.warn(f"{ev.file_id}: unrecognised JSON shape {sorted(doc)}")


def _json_snapshots(ev: EvidenceFile, sink: Sink, doc: dict) -> None:
    for si, snap in enumerate(doc["snapshots"]):
        captured = snap.get("captured_at")
        store, coll = snap.get("data_store"), snap.get("collection")
        lookup_like = coll == "operational_record_history"
        for ri, rec in enumerate(snap.get("records", [])):
            rid = rec.get("resource_id")
            fields = {k: v for k, v in flatten(rec).items() if k != "resource_id"}
            when, when_src = (rec["recorded_at"], "record") if rec.get("recorded_at") else (captured, "snapshot")
            sink.observe(
                ev, resource_id=rid, fields=fields, obs_time=when, obs_time_source=when_src,
                store_role="readback" if lookup_like else "snapshot", container=coll,
                system_id=rec.get("system") or (system_for_resource(rid) if lookup_like else store),
                explicit_type=rec.get("record_type"), explicit_result=rec.get("result"),
                presence="PRESENT", source_ref=f"{ev.path}#/snapshots/{si}/records/{ri}",
            )
            _alias_fields(sink, ev, rid, fields)
        if not lookup_like and store and captured:
            sink.scope(ev, store, "snapshot", coll, captured, "collection_snapshot", len(snap.get("records", [])))


def _json_service_exports(ev: EvidenceFile, sink: Sink, doc: dict) -> None:
    exported = doc.get("exported_at") or ev.captured_at
    for system, collections in doc["service_exports"].items():
        for coll, records in collections.items():
            for ri, rec in enumerate(records):
                ref = f"{ev.path}#/service_exports/{system}/{coll}/{ri}"
                when = rec.get("observed_at")
                if coll == "index_queries":
                    query_ids = extract_identifiers(rec.get("query"), "query")
                    docs = (rec.get("hits") or {}).get("documents") or []
                    for di, hit in enumerate(docs):
                        fields = {k: v for k, v in hit.items() if k != "resource_id"}
                        fields["query"] = rec.get("query")
                        obs = sink.observe(ev, resource_id=hit.get("resource_id"), fields=fields, obs_time=when,
                                           obs_time_source="record", store_role="readback", container=coll,
                                           system_id=system, presence="PRESENT", source_ref=f"{ref}/hits/{di}")
                        obs.add_identifiers(query_ids, "query", "query")
                    if not docs:
                        obs = sink.observe(ev, resource_id=rec.get("resource_id"), fields={"query": rec.get("query")},
                                           obs_time=when, obs_time_source="record", store_role="readback",
                                           container=coll, system_id=system, presence="ABSENT", source_ref=ref)
                        obs.add_identifiers(query_ids, "query", "query")
                    continue
                if coll == "profile_lookups":
                    fields = flatten(rec.get("profile") or {})
                    fields["status_code"] = rec.get("status_code")
                    presence = "ABSENT" if rec.get("status_code") == 404 else "PRESENT"
                else:
                    fields = {k: v for k, v in rec.items() if k not in ("resource_id", "observed_at")}
                    presence = "PRESENT"
                sink.observe(ev, resource_id=rec.get("resource_id"), fields=fields, obs_time=when,
                             obs_time_source="record", store_role="readback", container=coll, system_id=system,
                             presence=presence, source_ref=ref)
            sink.scope(ev, system, "readback", coll, exported, "query_export", len(records))


def _json_lookup_records(ev: EvidenceFile, sink: Sink, doc: dict) -> None:
    for ri, rec in enumerate(doc["records"]):
        fields = {k: v for k, v in rec.items() if k not in ("resource_id", "recorded_at", "system")}
        sink.observe(ev, resource_id=rec.get("resource_id"), fields=fields, obs_time=rec.get("recorded_at"),
                     obs_time_source="record", store_role="readback", container="record_lookup",
                     system_id=rec.get("system"), explicit_type=rec.get("record_type"),
                     explicit_result=rec.get("result"), source_ref=f"{ev.path}#/records/{ri}")


def _json_collection(ev: EvidenceFile, sink: Sink, doc: dict) -> None:
    captured = doc.get("captured_at")
    coll = doc["collection"]
    systems = set()
    for ri, rec in enumerate(doc["records"]):
        rid = rec.get("resource_id")
        fields = {k: v for k, v in flatten(rec).items() if k != "resource_id"}
        obs = sink.observe(ev, resource_id=rid, fields=fields, obs_time=captured, obs_time_source="snapshot",
                           store_role="snapshot", container=coll, presence="PRESENT",
                           source_ref=f"{ev.path}#/records/{ri}")
        _alias_fields(sink, ev, rid, fields)
        if obs.system_id:
            systems.add(obs.system_id)
    for system in sorted(systems):
        sink.scope(ev, system, "snapshot", coll, captured or ev.captured_at, "collection_snapshot", len(doc["records"]))


# ---------------------------------------------------------------------------
# SQL snapshots
# ---------------------------------------------------------------------------
ALLOWED_SQL = ("PRAGMA FOREIGN_KEYS", "BEGIN", "COMMIT", "CREATE TABLE", "CREATE INDEX", "CREATE UNIQUE INDEX",
               "INSERT INTO")
TYPED_TABLE_SYSTEM = {
    "customer_profiles": "account_service",
    "customer_search_documents": "search_service",
    "customer_analytics_profiles": "analytics_platform",
    "support_records": "support_platform",
    "customer_exports": "export_service",
    "connector_subscriptions": "integration_service",
    "backup_restore_entries": "backup_service",
    "security_audit_records": "security_service",
    "customer_profiles_primary": "account_service",
    "customer_profiles_failover_replica": "account_service",
    "search_documents_primary": "search_service",
}


def _store_role(table: str) -> str:
    if table.endswith("_primary"):
        return "primary"
    for role in ("failover_replica", "reporting_replica", "read_replica"):
        if table.endswith(role):
            return role
    m = re.search(r"_replica_(\w+)$", table)
    if m:
        return f"regional_replica:{m.group(1)}"
    return "inventory"


def _safe_sql_script(path: str) -> str:
    """Return the script only if every statement is DDL/INSERT (no destructive or attach statements)."""
    statements, buf = [], ""
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not buf and line.lstrip().startswith("--"):
                continue
            buf += line
            if sqlite3.complete_statement(buf):
                statements.append(buf.strip())
                buf = ""
    if buf.strip():
        raise ValueError("trailing incomplete SQL statement")
    for stmt in statements:
        head = " ".join(stmt.split()[:3]).upper()
        if not head.startswith(ALLOWED_SQL):
            raise ValueError(f"disallowed SQL statement: {head[:40]}")
    return "\n".join(statements)


def parse_sql(ev: EvidenceFile, sink: Sink) -> None:
    script = _safe_sql_script(ev.abs_path)
    header_time = None
    with open(ev.abs_path, encoding="utf-8") as fh:
        for line in fh:
            m = re.match(r"--\s*Captured at:\s*(\S+)", line)
            if m:
                header_time = m.group(1)
                break
            if not line.startswith("--"):
                break
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(script)
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        scope_times = {}
        if "snapshot_scope" in tables:
            scope_times = dict(db.execute("SELECT origin_system, captured_at FROM snapshot_scope"))
        blob_tables = [t for t in tables
                       if "record_json" in [c[1] for c in db.execute(f'PRAGMA table_info("{t}")')]]
        blob_systems = sorted({r[0] for t in blob_tables
                               for r in db.execute(f'SELECT DISTINCT origin_system FROM "{t}"')})
        for table in tables:
            if table == "snapshot_scope":
                continue
            role = _store_role(table)
            cols = [c[1] for c in db.execute(f'PRAGMA table_info("{table}")')]
            rows = db.execute(f'SELECT * FROM "{table}" ORDER BY resource_id').fetchall()
            if table in blob_tables:
                counts = defaultdict(int)
                for row in rows:
                    rec = dict(zip(cols, row))
                    body = json.loads(rec["record_json"])
                    fields = {k: v for k, v in flatten(body).items() if k != "resource_id"}
                    fields["row_updated_at"] = rec.get("updated_at")
                    sink.observe(ev, resource_id=rec["resource_id"], fields=fields, obs_time=header_time,
                                 obs_time_source="header", store_role=role, container=rec["collection_name"],
                                 system_id=rec["origin_system"], presence="PRESENT",
                                 source_ref=f"{ev.path}#{table}/{rec['resource_id']}")
                    _alias_fields(sink, ev, rec["resource_id"], fields)
                    counts[rec["origin_system"]] += 1
                for system in blob_systems:
                    sink.scope(ev, system, role, table, header_time or ev.captured_at, "table_snapshot",
                               counts.get(system, 0))
                continue
            system = TYPED_TABLE_SYSTEM.get(table)
            when = scope_times.get(system) or header_time
            when_src = "snapshot" if scope_times.get(system) else "header"
            for row in rows:
                rec = dict(zip(cols, row))
                rid = rec.pop("resource_id")
                for flag in ("enabled", "restore_allowed"):
                    if flag in rec and rec[flag] is not None:
                        rec[flag] = bool(rec[flag])
                if rec.get("source_resource_id"):
                    sink.link(rid, rec["source_resource_id"], "indexed_from", ev.file_id, when)
                presence = "PRESENT"
                sink.observe(ev, resource_id=rid, fields=rec, obs_time=when, obs_time_source=when_src,
                             store_role=role, container=table, system_id=system or system_for_resource(rid),
                             presence=presence, source_ref=f"{ev.path}#{table}/{rid}")
            if system:
                sink.scope(ev, system, role, table, when or ev.captured_at, "table_snapshot", len(rows))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Plain-text handover
# ---------------------------------------------------------------------------
HANDOVER_RE = re.compile(r"^- (\S+) \| (\S+) \| (\S+) \| (\S+) \| (\S+) \| (.*)$")


def parse_txt(ev: EvidenceFile, sink: Sink) -> None:
    matched = 0
    with open(ev.abs_path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            m = HANDOVER_RE.match(line.strip())
            if not m:
                continue
            ts, system, rid, rtype, result, subject = m.groups()
            matched += 1
            sink.observe(ev, resource_id=rid, fields={"subject_reference": subject.strip(), "result": result},
                         obs_time=ts, obs_time_source="record", store_role="readback", container="handover_check",
                         system_id=system, explicit_type=rtype, explicit_result=result,
                         source_ref=f"{ev.path}:{lineno}")
    if not matched:
        sink.warn(f"{ev.file_id}: no handover lines recognised")


# ---------------------------------------------------------------------------
# PDF documents
# ---------------------------------------------------------------------------
def _pdf_text(path: str) -> str | None:
    try:
        from pypdf import PdfReader  # optional dependency (requirements.txt)
        return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    except ImportError:
        pass
    exe = shutil.which("pdftotext")
    if exe:
        out = subprocess.run([exe, "-layout", path, "-"], capture_output=True, check=True)
        return out.stdout.decode("utf-8", errors="replace")
    return None


def parse_pdf(ev: EvidenceFile, sink: Sink) -> bool:
    text = _pdf_text(ev.abs_path)
    if text is None:
        sink.warn(f"{ev.file_id}: no PDF text extractor available (install pypdf); document not indexed")
        return False
    subjects = {i for i, k in extract_identifiers(text) if k in ("email", "user_ref")}
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    row_lines = [ln for ln in lines if (find_resources(ln) or find_aliases(ln)) and extract_identifiers(ln)]
    if len(subjects) > 1 and row_lines:
        # Multi-subject register: one observation per row that carries both a reference and a subject.
        for idx, ln in enumerate(row_lines):
            _pdf_observation(ev, sink, ln, f"{ev.path}#row{idx + 1}", register_row=True)
    else:
        _pdf_observation(ev, sink, text, f"{ev.path}#document", register_row=False)
    return True


def _pdf_observation(ev: EvidenceFile, sink: Sink, text: str, ref: str, register_row: bool) -> None:
    resources = find_resources(text)
    aliases = find_aliases(text)
    retention_row = register_row and re.search(r"\bretain until\b", text, re.I)
    container = "retention_register_row" if retention_row else "document"
    fields = {"document_text": text, "aliases": [a for a, _k in aliases]}
    targets = resources or [None]
    for rid in targets:
        sink.observe(ev, resource_id=rid, fields=fields, obs_time=None, obs_time_source="manifest",
                     store_role="document", container=container, presence="PRESENT", source_ref=ref)


# ---------------------------------------------------------------------------
# Images (cached vision extraction)
# ---------------------------------------------------------------------------
def load_media_extractions(path) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    return data.get("files", {})


def parse_image(ev: EvidenceFile, sink: Sink, extractions: dict) -> bool:
    entry = extractions.get(ev.file_id)
    if not entry:
        return False
    for idx, item in enumerate(entry.get("observations", [])):
        fields = dict(item.get("fields") or {})
        rid = item.get("resource_id")
        if rid and not RESOURCE_RE.fullmatch(rid):
            # A screen showing a file name or user id instead of a resource id: resolved later
            # through resource_aliases (e.g. delivery_confirmation_19.pdf -> object_19).
            fields["aliases"] = [rid]
            rid = None
        sink.observe(ev, resource_id=rid, fields=fields, obs_time=item.get("obs_time"),
                     obs_time_source="record" if item.get("obs_time") else "manifest", store_role="image",
                     container=item.get("container", "screen"), system_id=item.get("system_id"),
                     explicit_type=item.get("record_type"), presence=item.get("presence", "PRESENT"),
                     source_ref=f"{ev.path}#item{idx + 1}")
    return True
