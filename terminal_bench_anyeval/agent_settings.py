"""Single source of the Terminus 2 protocol carried in spec.agent_kwargs."""
from copy import deepcopy
from types import MappingProxyType

PINNED_AGENT_KWARGS = MappingProxyType({
    "parser_name": "json",
    "record_terminal_session": True,
    "enable_summarize": True,
    "proactive_summarization_threshold": 8000,
    "use_responses_api": False,
    "llm_backend": "litellm",
})


def agent_kwargs(provider: dict | None = None) -> dict:
    """Build the runner's spec settings without credentials or model endpoints."""
    result = dict(PINNED_AGENT_KWARGS)
    if provider is not None:
        result["llm_call_kwargs"] = {"extra_body": {"provider": deepcopy(provider)}}
    return result


def validate_agent_kwargs(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("agent_kwargs must be a mapping")
    if set(value) - set(PINNED_AGENT_KWARGS) - {"llm_call_kwargs"}:
        raise ValueError("Unsupported agent setting; secrets and llm_kwargs are forbidden")
    for key, pinned in PINNED_AGENT_KWARGS.items():
        if key not in value or type(value[key]) is not type(pinned) or value[key] != pinned:
            raise ValueError(f"agent_kwargs.{key} must match the pinned protocol")
    call = value.get("llm_call_kwargs", {})
    if not isinstance(call, dict) or set(call) - {"extra_body"}:
        raise ValueError("Only the gateway provider pin is accepted in llm_call_kwargs")
    body = call.get("extra_body", {})
    if not isinstance(body, dict) or set(body) - {"provider"}:
        raise ValueError("Only extra_body.provider is accepted")
    if "provider" in body:
        provider = body["provider"]
        if not isinstance(provider, dict) or set(provider) - {"only", "order", "allow_fallbacks"}:
            raise ValueError("Invalid provider pin")
        for key in ("only", "order"):
            if key in provider and (not isinstance(provider[key], list) or not provider[key]
                                   or any(not isinstance(p, str) or not p for p in provider[key])):
                raise ValueError("Invalid provider pin")
        if "allow_fallbacks" in provider and type(provider["allow_fallbacks"]) is not bool:
            raise ValueError("Invalid provider fallback pin")
    return deepcopy(value)
