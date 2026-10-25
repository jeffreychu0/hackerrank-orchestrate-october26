"""In-memory containers that parsers fill and the builder writes to SQLite."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .normalize import derive_state, identifiers_from_fields, record_type_for, system_for_resource


@dataclass
class EvidenceFile:
    file_id: str
    path: str            # relative to the dataset dir, as in the manifest
    abs_path: str
    origin: str
    captured_at: str


@dataclass
class Observation:
    file_id: str
    file_captured_at: str
    obs_time: str
    obs_time_source: str
    system_id: str | None
    resource_id: str | None
    record_type: str | None
    record_type_source: str
    store_role: str
    derived_state: str
    container: str | None
    trace_id: str | None
    fields: dict
    source_ref: str
    identifiers: set = field(default_factory=set)   # {(identifier, kind, field, link_via)}

    def add_identifiers(self, items, link_via: str, field_name: str | None = None) -> None:
        for item in items:
            if len(item) == 3:
                ident, kind, fld = item
            else:
                ident, kind = item
                fld = field_name or "?"
            self.identifiers.add((ident, kind, fld, link_via))


class Sink:
    """Collects everything one build produces. Parsers only ever append here."""

    def __init__(self) -> None:
        self.observations: list[Observation] = []
        self.scopes: dict[tuple, dict] = {}
        self.aliases: set[tuple[str, str, str, str]] = set()
        self.links: set[tuple[str, str, str, str, str | None]] = set()
        self.warnings: list[str] = []
        self.file_notes: dict[str, str] = {}

    def observe(self, ev: EvidenceFile, *, resource_id: str | None, fields: dict, obs_time: str | None,
                obs_time_source: str, store_role: str, container: str | None = None,
                system_id: str | None = None, explicit_type: str | None = None, event: str | None = None,
                presence: str | None = None, explicit_result: str | None = None,
                trace_id: str | None = None, source_ref: str = "", state: str | None = None) -> Observation:
        if not obs_time:
            obs_time, obs_time_source = ev.captured_at, "manifest"
        system_id = system_id or system_for_resource(resource_id)
        rtype, rsource = record_type_for(resource_id, container, event, explicit_type)
        derived = state or derive_state(rtype, fields, presence, explicit_result)
        obs = Observation(
            file_id=ev.file_id, file_captured_at=ev.captured_at, obs_time=obs_time,
            obs_time_source=obs_time_source, system_id=system_id, resource_id=resource_id,
            record_type=rtype, record_type_source=rsource, store_role=store_role,
            derived_state=derived, container=container, trace_id=trace_id, fields=fields,
            source_ref=source_ref or ev.path,
        )
        obs.add_identifiers(identifiers_from_fields(fields), "direct")
        self.observations.append(obs)
        return obs

    def scope(self, ev: EvidenceFile, system_id: str, store_role: str, container: str, captured_at: str,
              scope_kind: str, row_count: int) -> None:
        key = (ev.file_id, system_id, store_role, container)
        prev = self.scopes.get(key)
        self.scopes[key] = {
            "file_id": ev.file_id, "system_id": system_id, "store_role": store_role, "container": container,
            "captured_at": captured_at, "scope_kind": scope_kind,
            "row_count": row_count + (prev["row_count"] if prev else 0),
        }

    def alias(self, alias: str, kind: str, resource_id: str, source: str) -> None:
        if alias and resource_id and alias != resource_id:
            self.aliases.add((alias, kind, resource_id, source))

    def link(self, resource_id: str, related: str, relation: str, source: str, when: str | None = None) -> None:
        if resource_id and related and resource_id != related:
            self.links.add((resource_id, related, relation, source, when))

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
