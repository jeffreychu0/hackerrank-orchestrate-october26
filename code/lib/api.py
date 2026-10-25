"""Claude API access: credentials, client construction, and one structured-output call helper.

Every model call in the pipeline goes through `structured_call`, so refusal/truncation handling,
error classification, and usage logging live in one place.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from lib.config import REPO_ROOT
from lib.logger import log_usage

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# List prices, USD per million tokens (input, output), for cost estimates in the usage report.
PRICE_PER_MTOK = {"claude-opus-5-5": (4.00, 20.00)}


class CredentialsRejected(RuntimeError):
    """The API rejected our credentials; callers should stop issuing requests."""


@dataclass
class CallResult:
    data: dict | None                 # parsed JSON matching the requested schema, or None on failure
    model: str | None = None          # the model that actually served the request (fallback-aware)
    stop_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    flags: list[str] = field(default_factory=list)


def load_dotenv(path: str = os.path.join(REPO_ROOT, ".env")) -> None:
    """Load KEY=VALUE lines from .env into os.environ without overriding existing values."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def have_credentials() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                or os.environ.get("ANTHROPIC_PROFILE")
                or os.path.isdir(os.path.join(os.path.expanduser("~"), ".config", "anthropic")))


def get_client():
    import anthropic
    return anthropic.Anthropic()


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    price_in, price_out = PRICE_PER_MTOK.get(model, PRICE_PER_MTOK[DEFAULT_MODEL])
    return input_tokens * price_in / 1e6 + output_tokens * price_out / 1e6


def structured_call(client, *, stage: str, system: str, user_content: str | list, schema: dict, effort: str = "low",
                    max_tokens: int = 2048, model: str = DEFAULT_MODEL, log_extra: dict | None = None) -> CallResult:
    """One Messages API call constrained to `schema`. Never raises for per-request failures; returns flags."""
    import anthropic

    try:
        response = client.beta.messages.create(
            model=model,
            max_tokens=max_tokens,
            betas=[FALLBACK_BETA],
            fallbacks="default",           # a declined request is re-run server-side on the recommended model
            system=system,
            messages=[{"role": "user", "content": user_content}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        )
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        raise CredentialsRejected(f"credentials rejected ({type(exc).__name__})") from exc
    except anthropic.BadRequestError as exc:
        return CallResult(None, flags=[f"BAD_REQUEST: {exc.message}"])
    except anthropic.APIStatusError as exc:     # 429/5xx are already retried by the SDK
        return CallResult(None, flags=[f"API_ERROR_{exc.status_code}"])
    except anthropic.APIConnectionError:
        return CallResult(None, flags=["CONNECTION_ERROR"])

    usage = response.usage
    result = CallResult(None, model=response.model, stop_reason=response.stop_reason,
                        input_tokens=usage.input_tokens or 0, output_tokens=usage.output_tokens or 0)
    log_usage({"stage": stage, "model_requested": model, "model_served": response.model,
               "stop_reason": response.stop_reason, "input_tokens": result.input_tokens,
               "output_tokens": result.output_tokens,
               "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0, **(log_extra or {})})
    if response.stop_reason == "refusal":
        result.flags.append("REFUSED")
        return result
    if response.stop_reason == "max_tokens":
        result.flags.append("TRUNCATED")
        return result
    text = next((b.text for b in response.content if b.type == "text"), None)
    try:
        result.data = json.loads(text or "")
    except json.JSONDecodeError:
        result.flags.append("INVALID_JSON")
    return result
