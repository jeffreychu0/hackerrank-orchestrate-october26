"""Stage 4 runner: one Strands agent per flagged case, cached, with a safe fallback.

The agent only sees one case (case-scoped tools), must submit a report that passes the stage-5
validator, and is bounded by turn and token limits. Anything short of an ACCEPTED report leaves
the deterministic fallback report in place and flags the case.
"""
from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from lib import api, logger
from lib.config import CACHE_DIR
from stages.adjudicator.prompts import PROMPT_VERSION, SYSTEM_PROMPT, case_prompt

AGENT_MODEL = api.DEFAULT_MODEL
AGENT_EFFORT = "medium"          # multi-step tool use over a small case; see effort guidance in docs
AGENT_LIMITS = {"turns": 20, "total_tokens": 400_000}
CACHE_PATH = os.path.join(CACHE_DIR, "adjudication_cache.json")


def _compact_bundle(case: dict, bundle: dict, draft: dict) -> dict:
    subject = bundle.get("subject") or {}
    return {
        "case_id": bundle["case_id"], "audit_at": bundle["audit_at"], "requested_at": case["requested_at"],
        "request_text": case["request_text"],
        "subject": {"user_id": subject.get("user_id"),
                    "identifiers": [{"kind": i.get("kind", i.get("id_kind")), "value": i.get("value", i.get("identifier")),
                                     "is_current": i.get("is_current")} for i in subject.get("identifiers", [])]},
        "resources": [{k: r[k] for k in ("resource_id", "system_id", "record_type", "latest_state", "latest_time",
                                         "latest_store_roles", "citable", "link_basis", "flags")}
                      | {"activity_claims": len(r["activity_claims"])} for r in bundle["resources"]],
        "excluded_by_retrieval": bundle.get("excluded", []),
        "draft_report": {"resources": draft["resources"], "excluded": draft["excluded"]},
    }


def _cache_key(compact: dict, reasons: list[str]) -> str:
    payload = json.dumps({"v": PROMPT_VERSION, "model": AGENT_MODEL, "effort": AGENT_EFFORT, "case": compact,
                          "reasons": reasons}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_cache() -> dict:
    try:
        with open(CACHE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}


def _save_cache(cache: dict) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, CACHE_PATH)


def _build_model():
    from strands.models.anthropic import AnthropicModel
    return AnthropicModel(
        client_args={"default_headers": {"anthropic-beta": api.FALLBACK_BETA}},
        model_id=AGENT_MODEL,
        max_tokens=16000,
        params={"output_config": {"effort": AGENT_EFFORT}, "extra_body": {"fallbacks": "default"}},
    )


def _run_one(case: dict, bundle: dict, draft: dict, reasons: list[str], rules: list[dict], compact: dict) -> dict:
    from strands import Agent
    from stages.adjudicator.tools import CaseTools

    ctx = CaseTools(case, bundle, rules)
    meta = {"case_id": bundle["case_id"], "reasons": reasons, "flags": [], "input_tokens": 0, "output_tokens": 0,
            "turns": 0, "submissions": 0, "stop_reason": None}
    try:
        agent = Agent(model=_build_model(), tools=ctx.tools, system_prompt=SYSTEM_PROMPT, callback_handler=None)
        result = agent(case_prompt(json.dumps(compact, ensure_ascii=False, indent=1), reasons), limits=AGENT_LIMITS)
        usage = result.metrics.accumulated_usage
        meta.update(input_tokens=usage.get("inputTokens", 0), output_tokens=usage.get("outputTokens", 0),
                    turns=result.metrics.cycle_count, stop_reason=str(result.stop_reason))
    except Exception as exc:  # API, credential, or framework error: keep the fallback
        meta["flags"].append(f"AGENT_ERROR:{type(exc).__name__}:{str(exc)[:160]}")
    finally:
        ctx.close()
    meta["submissions"] = ctx.submissions
    if ctx.tool_errors:
        meta["flags"].append(f"AGENT_TOOL_ERRORS:{len(ctx.tool_errors)}")
        meta["tool_errors"] = ctx.tool_errors[:5]
    logger.log_usage({"stage": "adjudicate", "prompt_version": PROMPT_VERSION, "model_requested": AGENT_MODEL,
                      "case_id": bundle["case_id"], "input_tokens": meta["input_tokens"],
                      "output_tokens": meta["output_tokens"], "turns": meta["turns"],
                      "stop_reason": meta["stop_reason"], "accepted": ctx.accepted_report is not None})
    if ctx.accepted_report is None:
        meta["flags"].append("AGENT_NO_ACCEPTED_REPORT")
        if ctx.last_errors:
            meta["last_errors"] = ctx.last_errors[:10]
    meta["report"] = ctx.accepted_report
    return meta


def adjudicate_cases(items: list[dict], rules: list[dict], use_model: bool = True, workers: int = 4) -> dict[str, dict]:
    """items: [{case, bundle, draft, reasons}]. Returns case_id -> {report (or None), meta}."""
    cache = _load_cache()
    out, pending = {}, []
    for item in items:
        compact = _compact_bundle(item["case"], item["bundle"], item["draft"])
        key = _cache_key(compact, item["reasons"])
        if key in cache:
            out[item["bundle"]["case_id"]] = {**cache[key], "source": "cache", "input_tokens": 0, "output_tokens": 0}
        else:
            pending.append((item, compact, key))
    if pending and not (use_model and api.have_credentials()):
        reason = "AGENT_DISABLED" if not use_model else "AGENT_NO_CREDENTIALS"
        for item, _c, _k in pending:
            out[item["bundle"]["case_id"]] = {"report": None, "flags": [reason], "reasons": item["reasons"],
                                              "source": "none", "input_tokens": 0, "output_tokens": 0}
        return out
    if pending:
        logger.info("stage 4", f"adjudicating {len(pending)} flagged cases with a Strands agent ({AGENT_MODEL}) ...")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_run_one, item["case"], item["bundle"], item["draft"], item["reasons"], rules,
                                   compact): (item, key) for item, compact, key in pending}
            for future in as_completed(futures):
                item, key = futures[future]
                meta = {**future.result(), "source": "model"}
                out[item["bundle"]["case_id"]] = meta
                if meta["report"] is not None:
                    cache[key] = {k: v for k, v in meta.items() if k not in ("source",)}
        _save_cache(cache)
    return out
