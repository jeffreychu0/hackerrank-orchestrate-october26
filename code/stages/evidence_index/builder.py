"""Builds cache/index.db from dataset/ (stage 0 of docs/pipeline.md)."""
from __future__ import annotations

import hashlib
import os
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone

from . import parsers
from .model import Sink, dumps
from .normalize import derive_state, identifiers_from_fields, record_type_for, system_for_resource
from .reference import load_activity, load_identities, load_manifest, load_requests, load_rules
from .schema import DDL, SCHEMA_VERSION

BUILDER_VERSION = "1.0.0"
KIND_BY_EXT = {".log": "log", ".json": "json", ".sql": "sql", ".txt": "txt", ".pdf": "pdf", ".png": "image",
               ".jpg": "image", ".jpeg": "image"}


def dataset_fingerprint(dataset: str, extra_files: list[str]) -> str:
    """SHA-256 over every dataset file (path + bytes) plus any extra inputs such as media extractions."""
    digest = hashlib.sha256()
    paths = []
    for root, _dirs, files in os.walk(dataset):
        for name in files:
            paths.append(os.path.join(root, name))
    for path in sorted(paths, key=lambda p: os.path.relpath(p, dataset).replace(os.sep, "/")):
        digest.update(os.path.relpath(path, dataset).replace(os.sep, "/").encode())
        with open(path, "rb") as fh:
            digest.update(hashlib.sha256(fh.read()).digest())
    for path in extra_files:
        if os.path.exists(path):
            with open(path, "rb") as fh:
                digest.update(b"extra:" + hashlib.sha256(fh.read()).digest())
    return digest.hexdigest()


def existing_fingerprint(out_path: str) -> dict:
    if not os.path.exists(out_path):
        return {}
    try:
        db = sqlite3.connect(f"file:{out_path}?mode=ro", uri=True)
        meta = dict(db.execute("SELECT key, value FROM build_meta"))
        db.close()
        return meta
    except sqlite3.Error:
        return {}


def _resolve_aliases(sink: Sink, origins: dict[str, str]) -> int:
    """Attach alias-only document/screen facts (ORD-/NST- references, file names, user ids) to the
    resource they name. When an alias names several resources (the same file name in two systems),
    keep only the candidate whose system is the evidence file's origin system, if exactly one is."""
    by_alias = defaultdict(set)
    for alias, _kind, rid, _src in sink.aliases:
        by_alias[alias].add(rid)
    resolved = 0
    for obs in sink.observations:
        if obs.resource_id or not obs.fields.get("aliases"):
            continue
        candidates = set()
        for alias in obs.fields["aliases"]:
            candidates |= by_alias.get(alias, set())
        if len(candidates) > 1:
            same_system = {c for c in candidates if system_for_resource(c) == origins.get(obs.file_id)}
            candidates = same_system if len(same_system) == 1 else candidates
        if len(candidates) != 1:
            continue
        rid = candidates.pop()
        obs.resource_id = rid
        obs.system_id = obs.system_id or system_for_resource(rid)
        if obs.record_type is None:
            obs.record_type, obs.record_type_source = record_type_for(rid, obs.container)
        obs.derived_state = derive_state(obs.record_type, obs.fields, "PRESENT")
        obs.fields = {**obs.fields, "resolved_via_alias": True}
        resolved += 1
    return resolved


def build(dataset: str, out_path: str, media_path: str, force: bool = False, log=print) -> dict:
    dataset = os.path.abspath(dataset)
    package_dir = os.path.dirname(os.path.abspath(__file__))
    source_files = sorted(os.path.join(package_dir, n) for n in os.listdir(package_dir) if n.endswith(".py"))
    fingerprint = dataset_fingerprint(dataset, [media_path, *source_files])
    prior = existing_fingerprint(out_path)
    if not force and prior.get("dataset_sha256") == fingerprint and prior.get("schema_version") == SCHEMA_VERSION \
            and prior.get("builder_version") == BUILDER_VERSION:
        log(f"Index is up to date ({out_path}); use --force to rebuild.")
        return {"skipped": True, **prior}

    sink = Sink()
    manifest = load_manifest(dataset)
    extractions = parsers.load_media_extractions(media_path)
    file_rows = []
    for ev in manifest:
        ext = os.path.splitext(ev.path)[1].lower()
        kind = KIND_BY_EXT.get(ext, "unknown")
        indexed, note = True, None
        before = len(sink.observations)
        try:
            if not os.path.exists(ev.abs_path):
                indexed, note = False, "missing on disk"
                sink.warn(f"{ev.file_id}: listed in manifest but missing on disk")
            elif kind == "log":
                parsers.parse_log(ev, sink)
            elif kind == "json":
                parsers.parse_json(ev, sink)
            elif kind == "sql":
                parsers.parse_sql(ev, sink)
            elif kind == "txt":
                parsers.parse_txt(ev, sink)
            elif kind == "pdf":
                indexed = parsers.parse_pdf(ev, sink)
                note = None if indexed else "no PDF text extractor"
            elif kind == "image":
                indexed = parsers.parse_image(ev, sink, extractions)
                note = None if indexed else "awaiting vision extraction (cache/media_extractions.json)"
            else:
                indexed, note = False, f"unsupported extension {ext}"
        except Exception as exc:  # a malformed file must not abort the whole build
            indexed, note = False, f"parse error: {type(exc).__name__}: {exc}"
            del sink.observations[before:]
            sink.warn(f"{ev.file_id}: {note}")
        if indexed and len(sink.observations) == before:
            note = "parsed, no resource observations"
        file_rows.append((ev.file_id, ev.path, ev.origin, ev.captured_at, kind, int(indexed), note))

    activity = load_activity(dataset, sink)
    resolved = _resolve_aliases(sink, {ev.file_id: ev.origin for ev in manifest})
    identities = load_identities(dataset)
    rules = load_rules(dataset)
    requests = load_requests(dataset)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    tmp_path = out_path + ".tmp"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    db = sqlite3.connect(tmp_path)
    try:
        db.executescript(DDL)
        db.executemany("INSERT INTO evidence_files VALUES (?,?,?,?,?,?,?)", file_rows)
        id_rows = []
        for obs_id, obs in enumerate(sink.observations, 1):
            db.execute(
                "INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (obs_id, obs.file_id, obs.file_captured_at, obs.obs_time, obs.obs_time_source, obs.system_id,
                 obs.resource_id, obs.record_type, obs.record_type_source, obs.store_role, obs.derived_state,
                 obs.container, obs.trace_id, dumps(obs.fields), obs.source_ref))
            for ident, kind, field, via in sorted(obs.identifiers):
                id_rows.append((obs_id, ident, kind, field, via))
        db.executemany("INSERT OR IGNORE INTO observation_identifiers VALUES (?,?,?,?,?)", id_rows)
        db.executemany("INSERT INTO snapshot_scopes VALUES (:file_id,:system_id,:store_role,:container,"
                       ":captured_at,:scope_kind,:row_count)", [sink.scopes[k] for k in sorted(sink.scopes)])
        db.executemany("INSERT INTO resource_aliases VALUES (?,?,?,?)", sorted(sink.aliases))
        db.executemany("INSERT INTO resource_links VALUES (?,?,?,?,?)",
                       sorted(sink.links, key=lambda r: tuple(x or "" for x in r)))
        db.executemany("INSERT INTO activity VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", activity)
        db.executemany("INSERT INTO identities VALUES (?,?,?,?,?,?,?)", identities)
        db.executemany("INSERT INTO deletion_rules VALUES (?,?,?,?)", rules)
        db.executemany("INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?)", requests)

        stats = {
            "observations": len(sink.observations),
            "observation_identifiers": db.execute("SELECT COUNT(*) FROM observation_identifiers").fetchone()[0],
            "snapshot_scopes": len(sink.scopes),
            "resource_aliases": len(sink.aliases),
            "resource_links": len(sink.links),
            "activity": len(activity),
            "identities": len(identities),
            "requests": len(requests),
            "aliases_resolved": resolved,
            "files_indexed": sum(r[5] for r in file_rows),
            "files_total": len(file_rows),
        }
        meta = {
            "schema_version": SCHEMA_VERSION,
            "builder_version": BUILDER_VERSION,
            "dataset_sha256": fingerprint,
            "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "media_extractions": os.path.basename(media_path) if extractions else "",
            "stats": dumps(stats),
            "warnings": dumps(sink.warnings),
        }
        db.executemany("INSERT INTO build_meta VALUES (?,?)", sorted(meta.items()))
        db.commit()
    finally:
        db.close()
    os.replace(tmp_path, out_path)

    by_kind = Counter(r[4] for r in file_rows)
    indexed_by_kind = Counter(r[4] for r in file_rows if r[5])
    log(f"Built {out_path}")
    log("  files: " + ", ".join(f"{k} {indexed_by_kind[k]}/{v}" for k, v in sorted(by_kind.items())))
    for key, value in stats.items():
        log(f"  {key}: {value}")
    states = Counter(o.derived_state for o in sink.observations)
    log("  derived_state: " + ", ".join(f"{k}={v}" for k, v in sorted(states.items())))
    for row in file_rows:
        if row[6]:
            log(f"  note {row[0]} ({row[1]}): {row[6]}")
    for w in sink.warnings:
        log(f"  warning: {w}")
    return {"skipped": False, **meta, "stats": stats}
