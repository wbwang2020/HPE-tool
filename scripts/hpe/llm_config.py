"""Independent LLM backend/key configuration.

API keys live only in a dedicated file (default
``~/.hpe-paper-evaluator/llm_apis.json`` or ``config/llm_apis.json``), never in
the model registry, run directory, report, or command line. The loader supports
literal keys and environment-variable fallbacks, but reports only provider/model
labels and whether a key is present.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import CONFIG_DIR
from .errors import ConfigError, CredentialError
from .util import read_json

DEFAULT_CONFIG_PATH = Path.home() / ".hpe-paper-evaluator" / "llm_apis.json"
AGENT_CONFIG_PATH = Path.home() / ".hpe-paper-evaluator" / "agent.json"
PACKAGE_CONFIG_PATH = CONFIG_DIR / "llm_apis.json"
CONFIG_SCHEMA = "hpe/llm-apis/v1"

BUILTIN_PROVIDERS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY",
        "default_model": "deepseek-chat",
        "models": {
            "deepseek-chat": {"context_window": 128000, "max_output_tokens": 8192, "json_mode": True, "price_per_mtok": {"input": 0.28, "output": 0.42}},
            "deepseek-reasoner": {"context_window": 128000, "max_output_tokens": 32768, "json_mode": True, "reasoning_effort": "high", "price_per_mtok": {"input": 0.55, "output": 2.19}},
        },
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key_env": "QIANWEN_API_KEY",
        "default_model": "qwen3-max",
        "models": {
            "qwen3-max": {"context_window": 131072, "max_output_tokens": 8192, "json_mode": True, "price_per_mtok": {"input": 0.0, "output": 0.0}},
            "qwen-max": {"context_window": 131072, "max_output_tokens": 8192, "json_mode": True, "price_per_mtok": {"input": 0.0, "output": 0.0}},
        },
    },
    "glm": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key_env": "GLM_API_KEY",
        "default_model": "glm-4-plus",
        "models": {
            "glm-4-plus": {"context_window": 128000, "max_output_tokens": 8192, "json_mode": True, "price_per_mtok": {"input": 0.0, "output": 0.0}},
            "glm-5.2": {"context_window": 1000000, "max_output_tokens": 131072, "json_mode": True, "price_per_mtok": {"input": 0.0, "output": 0.0}},
        },
    },
    "kimi": {
        "base_url": "https://api.moonshot.cn/v1",
        "api_key_env": "MOONSHOT_API_KEY",
        "default_model": "kimi-k2-0905-preview",
        "models": {
            "kimi-k2-0905-preview": {"context_window": 131072, "max_output_tokens": 8192, "json_mode": True, "price_per_mtok": {"input": 0.0, "output": 0.0}},
            "kimi-k2-turbo-preview": {"context_window": 131072, "max_output_tokens": 8192, "json_mode": True, "price_per_mtok": {"input": 0.0, "output": 0.0}},
            "moonshot-v1-128k": {"context_window": 128000, "max_output_tokens": 8192, "json_mode": True, "price_per_mtok": {"input": 0.0, "output": 0.0}},
        },
    },
}


@dataclass
class BackendSpec:
    kind: str = "host"  # host | api
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    context_window: int = 128000
    max_output_tokens: int = 8192
    json_mode: bool = True
    reasoning_effort: str | None = None
    price_input_per_mtok: float = 0.0
    price_output_per_mtok: float = 0.0
    source: str = "builtin"

    @property
    def is_host(self) -> bool:
        return self.kind == "host"

    @property
    def label(self) -> str:
        return "host" if self.is_host else f"{self.provider}:{self.model or 'default'}"

    def estimate_cost(self, tokens_in: int, tokens_out: int) -> float:
        return (
            tokens_in / 1_000_000.0 * self.price_input_per_mtok
            + tokens_out / 1_000_000.0 * self.price_output_per_mtok
        )

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        data = {
            "kind": self.kind,
            "label": self.label,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "api_key_present": bool(self.api_key),
            "api_key_env": self.api_key_env,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "json_mode": self.json_mode,
            "reasoning_effort": self.reasoning_effort,
            "source": self.source,
        }
        if not redact:
            data["api_key"] = self.api_key
        return data


@dataclass
class AgentIdentity:
    """The model that the host agent itself is running on.

    ``kind`` is ``host`` when the identity was observed from a host runtime
    (OpenCode / DeepSeek Harness), ``api`` when the user supplied an explicit
    provider:model, and ``unknown`` when no runtime clue exists. The provider
    and model are kept separately so consistency checks do not depend on the
    string format used by a particular host.
    """

    provider: str | None = None
    model: str | None = None
    kind: str = "unknown"
    source: str = "unknown"
    detected: bool = False
    raw: str | None = None

    @property
    def is_host(self) -> bool:
        return self.kind == "host"

    @property
    def is_api(self) -> bool:
        return self.kind == "api"

    @property
    def label(self) -> str:
        if self.provider and self.model:
            return f"{self.provider}:{self.model}"
        return self.raw or "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "provider": self.provider,
            "model": self.model,
            "kind": self.kind,
            "source": self.source,
            "detected": self.detected,
        }


@dataclass
class LLMConfig:
    path: Path | None = None
    data: dict[str, Any] = field(default_factory=dict)
    permission_warning: str | None = None

    @property
    def providers(self) -> dict[str, Any]:
        merged = copy.deepcopy(BUILTIN_PROVIDERS)
        for name, entry in (self.data.get("providers") or {}).items():
            if name in merged:
                merged[name].update(entry)
            else:
                merged[name] = entry
        return merged

    @property
    def roles(self) -> dict[str, Any]:
        return dict(self.data.get("roles") or {})

    @property
    def default_backend(self) -> str:
        return str(self.data.get("default_backend") or "host")

    def status(self) -> dict[str, Any]:
        providers = {}
        for name, entry in self.providers.items():
            key = str(entry.get("api_key") or "")
            env_name = entry.get("api_key_env")
            providers[name] = {
                "base_url": entry.get("base_url"),
                "default_model": entry.get("default_model"),
                "key_configured": bool(key or (env_name and os.environ.get(env_name))),
                "key_source": "config" if key else ("env" if env_name and os.environ.get(env_name) else "missing"),
            }
        return {
            "path": str(self.path) if self.path else None,
            "permission_warning": self.permission_warning,
            "default_backend": self.default_backend,
            "providers": providers,
            "roles": self.roles,
        }


def _candidate_paths(explicit: str | Path | None) -> list[Path]:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    if os.environ.get("HPE_LLM_CONFIG"):
        candidates.append(Path(os.environ["HPE_LLM_CONFIG"]).expanduser())
    candidates.append(DEFAULT_CONFIG_PATH)
    candidates.append(PACKAGE_CONFIG_PATH)
    return candidates


def _permission_warning(path: Path) -> str | None:
    if os.name == "nt":
        return None
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None
    if mode & 0o077:
        return f"{path} is readable by group/other (mode {oct(mode)}); chmod 600 is recommended"
    return None


def load_llm_config(explicit: str | Path | None = None, *, required: bool = False) -> LLMConfig:
    for candidate in _candidate_paths(explicit):
        if not candidate.is_file():
            continue
        try:
            data = read_json(candidate)
        except (OSError, ValueError) as exc:
            raise ConfigError(f"Cannot read LLM config {candidate}: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"LLM config {candidate} must be a JSON object")
        if data.get("schema_version") not in (None, CONFIG_SCHEMA):
            raise ConfigError(f"Unsupported LLM config schema {data.get('schema_version')!r} in {candidate}")
        return LLMConfig(path=candidate.resolve(), data=data, permission_warning=_permission_warning(candidate))
    if explicit or required:
        raise ConfigError(
            f"LLM config file not found: {explicit or DEFAULT_CONFIG_PATH}. "
            "Create it with `python scripts/hpe.py init-llm-config`."
        )
    return LLMConfig(path=None, data={})


def _provider_entry(config: LLMConfig, provider: str) -> dict[str, Any]:
    providers = config.providers
    if provider not in providers:
        raise ConfigError(f"Unknown LLM provider {provider!r}. Known: {', '.join(sorted(providers))}")
    return providers[provider]


def _read_key(entry: dict[str, Any]) -> str | None:
    literal = str(entry.get("api_key") or "").strip()
    if literal:
        return literal
    env_name = entry.get("api_key_env")
    if env_name and os.environ.get(str(env_name)):
        return os.environ[str(env_name)]
    return None


def backend_from_spec(
    spec: str | None,
    config: LLMConfig | None = None,
    *,
    default_provider: str | None = None,
    default_model: str | None = None,
) -> BackendSpec:
    """Parse ``host`` or ``provider[:model]`` into a BackendSpec."""

    config = config or LLMConfig()
    raw = (spec or "").strip()
    if not raw:
        if default_provider and default_provider.lower() != "host":
            raw = default_provider if not default_model else f"{default_provider}:{default_model}"
        else:
            return BackendSpec(kind="host", source="default")
    if raw.lower() == "host":
        return BackendSpec(kind="host", source="explicit")
    value = raw
    if ":" in value:
        provider, model = value.split(":", 1)
    else:
        provider, model = value, ""
    provider = provider.strip().lower()
    try:
        entry = _provider_entry(config, provider)
    except ConfigError:
        # Backward-compatible fallback to config/models.json (used by the
        # local stub provider and pre-v3 API runs).
        from .config import read_api_key, resolve_model

        legacy = resolve_model(provider, model.strip() or default_model)
        return BackendSpec(
            kind="api",
            provider=legacy.provider,
            model=legacy.model,
            base_url=legacy.base_url,
            api_key=read_api_key(legacy.api_key_env),
            api_key_env=legacy.api_key_env,
            context_window=legacy.context_window,
            max_output_tokens=legacy.max_output_tokens,
            json_mode=legacy.json_mode,
            reasoning_effort=legacy.reasoning_effort,
            price_input_per_mtok=legacy.price_input_per_mtok,
            price_output_per_mtok=legacy.price_output_per_mtok,
            source="config/models.json",
        )
    model_name = model.strip() or default_model or str(entry.get("default_model") or "")
    models = entry.get("models") or {}
    model_entry = dict(models.get(model_name) or {})
    if not model_name:
        raise ConfigError(f"Provider {provider} has no model and no default_model")
    price = model_entry.get("price_per_mtok") or entry.get("price_per_mtok") or {}
    return BackendSpec(
        kind="api",
        provider=provider,
        model=model_name,
        base_url=str(entry.get("base_url") or ""),
        api_key=_read_key(entry),
        api_key_env=entry.get("api_key_env"),
        context_window=int(model_entry.get("context_window") or entry.get("context_window") or 128000),
        max_output_tokens=int(model_entry.get("max_output_tokens") or 8192),
        json_mode=bool(model_entry.get("json_mode", True)),
        reasoning_effort=model_entry.get("reasoning_effort") or entry.get("reasoning_effort"),
        price_input_per_mtok=float(price.get("input", 0.0)),
        price_output_per_mtok=float(price.get("output", 0.0)),
        source=str(config.path) if config.path else "builtin",
    )


def require_api_key(spec: BackendSpec) -> BackendSpec:
    if spec.is_host:
        return spec
    if not spec.api_key and not spec.api_key_env:
        # Local/stub providers in config/models.json may explicitly have no key.
        return spec
    if not spec.api_key:
        env_hint = f" or set {spec.api_key_env}" if spec.api_key_env else ""
        raise CredentialError(
            f"No API key configured for {spec.provider}:{spec.model}. "
            f"Put it in the independent LLM config file (`api_key`){env_hint}; never pass keys on the command line."
        )
    return spec


def _identity_from_label(raw: str, *, source: str, detected: bool, kind: str | None = None) -> AgentIdentity:
    value = raw.strip()
    if not value or value.lower() == "host":
        return AgentIdentity(kind="host", source=source, detected=detected, raw="host")
    if ":" in value:
        provider, model = value.split(":", 1)
        provider = provider.strip().lower()
        model = model.strip()
    else:
        provider, model = value.strip().lower(), None
    return AgentIdentity(
        provider=provider or None,
        model=model or None,
        kind=kind or ("api" if provider else "unknown"),
        source=source,
        detected=detected,
        raw=raw,
    )


def _agent_from_config_file() -> AgentIdentity | None:
    if not AGENT_CONFIG_PATH.is_file():
        return None
    try:
        data = read_json(AGENT_CONFIG_PATH)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    backend = str(data.get("backend") or "").strip()
    if backend:
        return _identity_from_label(backend, source=str(AGENT_CONFIG_PATH), detected=True)
    provider = str(data.get("provider") or "").strip().lower()
    model = str(data.get("model") or "").strip()
    if provider and model:
        return AgentIdentity(
            provider=provider,
            model=model,
            kind=str(data.get("kind") or "api"),
            source=str(AGENT_CONFIG_PATH),
            detected=True,
            raw=f"{provider}:{model}",
        )
    return None


def _agent_from_opencode() -> AgentIdentity | None:
    """Ask OpenCode which model the current session is using.

    This is an optional, best-effort probe. It is only attempted inside an
    OpenCode runtime and never reads credentials.
    """

    if not (os.environ.get("OPENCODE") or os.environ.get("OPENCODE_SESSION_ID")):
        return None
    session_id = os.environ.get("OPENCODE_SESSION_ID")
    binary = shutil.which("opencode")
    if not session_id or not binary:
        return None
    try:
        completed = subprocess.run(
            [binary, "api", "get", f"/api/session/{session_id}"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    try:
        payload = json.loads(completed.stdout)
    except ValueError:
        return None
    model = ((payload.get("data") or {}).get("model") or {}) if isinstance(payload, dict) else {}
    provider = str(model.get("providerID") or "").strip().lower()
    model_id = str(model.get("id") or "").strip()
    if not provider or not model_id:
        return None
    return AgentIdentity(
        provider=provider,
        model=model_id,
        kind="host",
        source="opencode-session",
        detected=True,
        raw=f"{provider}:{model_id}",
    )


def _codex_config_model() -> str | None:
    config_path = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")) / "config.toml"
    if not config_path.is_file():
        return None
    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r'(?m)^\s*model\s*=\s*["\']([^"\']+)["\']', text)
    return match.group(1).strip() if match else None


def _agent_from_codex() -> AgentIdentity | None:
    """Best-effort Codex CLI identity detection (no auth/secret reads)."""

    if not (os.environ.get("CODEX_HOME") or os.environ.get("CODEX") or os.environ.get("CODEX_SESSION_ID") or (Path.home() / ".codex").is_dir()):
        return None
    model = str(os.environ.get("CODEX_MODEL") or "").strip() or _codex_config_model()
    if not model:
        return None
    provider = str(os.environ.get("CODEX_PROVIDER") or "openai").strip().lower()
    return AgentIdentity(
        provider=provider,
        model=model,
        kind="host",
        source="codex",
        detected=True,
        raw=f"{provider}:{model}",
    )


def _agent_from_harness_env() -> AgentIdentity | None:
    backend = str(os.environ.get("HPE_AGENT_BACKEND") or "").strip()
    if backend:
        return _identity_from_label(backend, source="HPE_AGENT_BACKEND", detected=True)
    provider = str(os.environ.get("HPE_AGENT_PROVIDER") or "").strip().lower()
    model = str(os.environ.get("HPE_AGENT_MODEL") or "").strip()
    harness_model = str(os.environ.get("DEEPSEEK_HARNESS_MODEL") or "").strip()
    if provider and model:
        return _identity_from_label(f"{provider}:{model}", source="HPE_AGENT_PROVIDER/HPE_AGENT_MODEL", detected=True, kind="host")
    if harness_model:
        if ":" in harness_model:
            return _identity_from_label(harness_model, source="DEEPSEEK_HARNESS_MODEL", detected=True, kind="host")
        return AgentIdentity(
            provider="deepseek",
            model=harness_model,
            kind="host",
            source="DEEPSEEK_HARNESS_MODEL",
            detected=True,
            raw=f"deepseek:{harness_model}",
        )
    return None


def detect_agent_identity(explicit: str | None = None) -> AgentIdentity:
    """Resolve the host-agent model, preferring an explicit user declaration."""

    if explicit:
        return _identity_from_label(explicit, source="--agent-backend", detected=True)
    for candidate in (
        _agent_from_harness_env(),
        _agent_from_opencode(),
        _agent_from_codex(),
        _agent_from_config_file(),
    ):
        if candidate is not None:
            return candidate
    return AgentIdentity(kind="unknown", source="not detected")


def _backend_from_agent(identity: AgentIdentity, config: LLMConfig) -> BackendSpec:
    if not identity.provider or not identity.model:
        raise ConfigError(
            "The host-agent model could not be detected. API mode needs an explicit "
            "`--agent-backend provider:model` (or HPE_AGENT_BACKEND) so the review "
            "model is the same as the agent model. Use `--mode host` to run on the "
            "current agent without an API key."
        )
    label = f"{identity.provider}:{identity.model}"
    try:
        spec = backend_from_spec(label, config)
    except ConfigError as exc:
        raise ConfigError(
            f"Agent backend {label} was detected, but it is not configured in the independent "
            f"LLM config ({config.path or DEFAULT_CONFIG_PATH}). API mode will not silently "
            "fall back to a different model of the same provider. Either:\n"
            "  1. use `--mode host` (recommended: the review runs on the current agent model, no key), or\n"
            f"  2. add provider `{identity.provider}` with base_url/model `{identity.model}` to "
            "llm_apis.json (and its api_key or api_key_env), then retry."
        ) from exc
    return spec


def _backend_vendor(provider: str | None, model: str | None) -> str:
    """Normalize provider gateways to the underlying model vendor.

    ``opencode-go:deepseek-v4.1-flash`` and ``deepseek:deepseek-chat`` are the
    same vendor; a review must not silently switch base model just because the
    gateway name differs.
    """

    provider_name = str(provider or "").strip().lower()
    model_name = str(model or "").strip().lower()
    for vendor in ("deepseek", "qwen", "glm", "kimi", "minimax", "mimo", "gpt", "claude", "gemini", "grok"):
        if model_name.startswith(vendor) or provider_name.startswith(vendor):
            return vendor
    if provider_name in {"opencode", "opencode-go"}:
        return "opencode"
    return provider_name or "unknown"


def backend_vendor(provider: str | None, model: str | None) -> str:
    """Public wrapper used by usage verification in the orchestrator."""

    return _backend_vendor(provider, model)


def _backend_consistency(
    *,
    agent: AgentIdentity,
    reviewer_specs: list[BackendSpec],
    author: BackendSpec,
    digest: BackendSpec,
    meta: BackendSpec,
    allow_mismatch: bool,
) -> dict[str, Any]:
    """Reject same-vendor/different-model defaults unless explicitly allowed."""

    roles: list[tuple[str, BackendSpec]] = [
        ("reviewer", spec) for spec in reviewer_specs
    ] + [("author", author), ("digest", digest), ("meta", meta)]
    mismatches: list[dict[str, str]] = []
    cross_provider: list[dict[str, str]] = []
    if agent.provider and agent.model:
        agent_vendor = _backend_vendor(agent.provider, agent.model)
        for role, spec in roles:
            if spec.is_host:
                continue
            role_vendor = _backend_vendor(spec.provider, spec.model)
            if role_vendor == agent_vendor and str(spec.model or "").lower() != str(agent.model or "").lower():
                mismatches.append(
                    {
                        "role": role,
                        "role_backend": spec.label,
                        "agent_backend": agent.label,
                        "reason": "same vendor, different base model",
                    }
                )
            elif role_vendor != agent_vendor:
                cross_provider.append(
                    {
                        "role": role,
                        "role_backend": spec.label,
                        "agent_backend": agent.label,
                        "reason": "different vendor (allowed, but the role no longer runs on the agent base)",
                    }
                )
    if mismatches and not allow_mismatch:
        detail = ", ".join(
            f"{item['role']}={item['role_backend']}" for item in mismatches
        )
        raise ConfigError(
            "Refusing a same-provider/different-model review base. The current agent is "
            f"{agent.label}, but these roles resolve to a different model of the same provider: {detail}. "
            "Leave role backends unset to inherit the agent model, or pass "
            "`--allow-backend-mismatch` if this is intentional."
        )
    return {
        "agent": agent.to_dict(),
        "consistent": not mismatches,
        "allow_mismatch": bool(allow_mismatch),
        "mismatches": mismatches,
        "cross_provider": cross_provider,
    }


def resolve_backends(
    *,
    config: LLMConfig,
    mode: str,
    default_provider: str | None,
    default_model: str | None,
    reviewer_backends: list[str] | None,
    author_backend: str | None,
    digest_backend: str | None,
    meta_backend: str | None,
    reviewer_count: int,
    agent_identity: AgentIdentity | None = None,
    allow_backend_mismatch: bool = False,
) -> dict[str, Any]:
    """Resolve role backends and enforce host/api mode and agent-base constraints."""

    agent = agent_identity or AgentIdentity(kind="unknown", source="not provided")
    global_default = None
    if mode == "api":
        # The default is the current agent model, never a hard-coded vendor
        # model. If the agent is host-only and not resolvable through the API
        # config, fail with guidance instead of silently using another model.
        if default_provider:
            # An explicit --provider/--model is an intentional override. It is
            # still checked against the agent identity below.
            global_default = default_provider if not default_model else f"{default_provider}:{default_model}"
        elif agent.provider and agent.model:
            global_default = _backend_from_agent(agent, config).label
        else:
            raise ConfigError(
                "API mode has no agent base. Pass `--agent-backend provider:model`, "
                "`--provider/--model`, or use `--mode host` (the current agent model)."
            )
    reviewer_specs = list(reviewer_backends or [])
    if not reviewer_specs:
        reviewer_specs = [""] * reviewer_count
    elif len(reviewer_specs) == 1:
        reviewer_specs = reviewer_specs * reviewer_count
    if len(reviewer_specs) != reviewer_count:
        raise ConfigError(f"--reviewer-backends has {len(reviewer_specs)} entries for {reviewer_count} reviewers")

    def resolve(value: str | None, fallback_default: bool = True) -> BackendSpec:
        if value:
            spec = backend_from_spec(value, config)
        elif fallback_default and global_default:
            spec = backend_from_spec(global_default, config)
        else:
            spec = backend_from_spec(None, config)
        if mode == "host" and not spec.is_host:
            raise ConfigError(
                f"Backend {spec.label} is external, but --mode host cannot call it. "
                "Use --mode api for external LLM APIs, or keep `host`."
            )
        if mode == "api" and spec.is_host:
            raise ConfigError(
                "API mode requires an external backend for every role; configure --reviewer-backends/--author-backend "
                "or --agent-backend/--provider, or use --mode host."
            )
        # hybrid intentionally allows both; the host and API loops filter by
        # task executor. mode=host rejects external above.

        if not spec.is_host:
            require_api_key(spec)
        return spec

    reviewers = [resolve(value) for value in reviewer_specs]
    author = resolve(author_backend or None)
    digest = resolve(digest_backend or None)
    meta = resolve(meta_backend or None)
    consistency = _backend_consistency(
        agent=agent,
        reviewer_specs=reviewers,
        author=author,
        digest=digest,
        meta=meta,
        allow_mismatch=allow_backend_mismatch,
    )
    return {"reviewers": reviewers, "author": author, "digest": digest, "meta": meta, "consistency": consistency}


def init_llm_config(path: str | Path | None = None) -> Path:
    target = Path(path).expanduser().resolve() if path else DEFAULT_CONFIG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return target
    payload = {
        "schema_version": CONFIG_SCHEMA,
        "default_backend": "host",
        "note": "Fill only the providers you use. Keep this file private (chmod 600). Never commit it.",
        "providers": {
            name: {
                "base_url": entry["base_url"],
                "api_key": "",
                "api_key_env": entry.get("api_key_env"),
                "default_model": entry["default_model"],
                "models": entry["models"],
            }
            for name, entry in BUILTIN_PROVIDERS.items()
        },
        "roles": {"reviewers": ["host", "host", "host"], "author": "host", "digest": "host", "meta": "host"},
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return target
