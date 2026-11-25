"""Configuration loading: profiles, model registry, package paths.

The package is deliberately runnable without a config directory: built-in
profiles and a conservative DeepSeek provider entry are used as fallbacks.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .util import read_json


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = PACKAGE_ROOT / "prompts"
SCHEMAS_DIR = PACKAGE_ROOT / "schemas"
CONFIG_DIR = PACKAGE_ROOT / "config"

PROMPT_VERSION = "3.1.0"
SCHEMA_VERSION = "hpe/v2"
SECTIONIZER_VERSION = "2.1.0"

MAX_REVIEWERS = 8
MAX_ROUNDS = 5
MAX_PDF_BYTES = 200 * 1024 * 1024
MAX_PDF_PAGES = 500

EVENT_LOG = "logs/events.jsonl"
USAGE_FILE = "logs/usage.json"


PURPOSE_IDS = ("review", "summary", "problem_finding")
MATERIAL_IDS = ("paper", "proposal", "design")

PERSONA_IDS = (
    "methodology",
    "experiments",
    "novelty",
    "reproducibility",
    "devils_advocate",
    "practitioner",
)

DEFAULT_REVIEWER_SETS: dict[str, list[str]] = {
    "paper": ["methodology", "experiments"],
    "conference": ["methodology", "experiments", "novelty"],
    "journal": ["methodology", "experiments", "novelty", "reproducibility"],
    "proposal": ["methodology", "novelty", "practitioner"],
    "experiment": ["experiments", "reproducibility", "devils_advocate"],
}

DEFAULT_PROFILES: dict[str, dict[str, Any]] = {
    "paper": {
        "description": "Generic paper, quick review",
        "weights": {
            "novelty": 0.20,
            "technical_soundness": 0.20,
            "evidence_strength": 0.20,
            "reproducibility": 0.10,
            "clarity": 0.10,
            "significance": 0.20,
        },
        "default_reviewers": DEFAULT_REVIEWER_SETS["paper"],
        "default_rounds": 0,
    },
    "conference": {
        "description": "CS/AI conference paper",
        "weights": {
            "novelty": 0.20,
            "technical_soundness": 0.25,
            "evidence_strength": 0.25,
            "reproducibility": 0.15,
            "clarity": 0.05,
            "significance": 0.10,
        },
        "default_reviewers": DEFAULT_REVIEWER_SETS["conference"],
        "default_rounds": 2,
    },
    "journal": {
        "description": "Journal paper; completeness and impact",
        "weights": {
            "novelty": 0.25,
            "technical_soundness": 0.25,
            "evidence_strength": 0.20,
            "reproducibility": 0.10,
            "clarity": 0.05,
            "significance": 0.15,
        },
        "default_reviewers": DEFAULT_REVIEWER_SETS["journal"],
        "default_rounds": 2,
    },
    "proposal": {
        "description": "Research proposal / thesis plan",
        "weights": {
            "novelty": 0.15,
            "technical_soundness": 0.25,
            "evidence_strength": 0.15,
            "reproducibility": 0.10,
            "clarity": 0.10,
            "significance": 0.25,
        },
        "default_reviewers": DEFAULT_REVIEWER_SETS["proposal"],
        "default_rounds": 1,
    },
    "experiment": {
        "description": "Experimental design / protocol",
        "weights": {
            "novelty": 0.10,
            "technical_soundness": 0.30,
            "evidence_strength": 0.30,
            "reproducibility": 0.15,
            "clarity": 0.05,
            "significance": 0.10,
        },
        "default_reviewers": DEFAULT_REVIEWER_SETS["experiment"],
        "default_rounds": 1,
    },
}

DEFAULT_DECISION_POLICY: dict[str, Any] = {
    "blocking_open": {"threshold": 1, "decision": "reject"},
    "major_open_gte": {"threshold": 1, "decision": "major_revision"},
    "minor_open_gte": {"threshold": 3, "decision": "minor_revision"},
    "default": "accept",
    "override_allowed": True,
    "min_override_confidence": 0.70,
    "single_reviewer_blocking_needs_meta": True,
}

DEFAULT_MODELS: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "default_provider": "deepseek",
    "providers": {
        "deepseek": {
            "type": "openai_compatible",
            "base_url": "https://api.deepseek.com/v1",
            "api_key_env": "DEEPSEEK_API_KEY",
            "default_model": "deepseek-chat",
            "models": {
                "deepseek-chat": {
                    "context_window": 128000,
                    "max_output_tokens": 8192,
                    "json_mode": True,
                    "reasoning_effort": None,
                    "price_per_mtok": {"input": 0.28, "output": 0.42},
                }
            },
        }
    },
}

SECTION_PRIORITY: dict[str, list[str]] = {
    "methodology": ["abstract", "introduction", "method", "theory", "experiments", "results", "appendix", "discussion", "conclusion", "related_work", "references", "other"],
    "experiments": ["abstract", "introduction", "experiments", "results", "method", "appendix", "discussion", "conclusion", "related_work", "theory", "references", "other"],
    "novelty": ["abstract", "introduction", "related_work", "method", "results", "discussion", "conclusion", "experiments", "theory", "appendix", "references", "other"],
    "reproducibility": ["abstract", "method", "experiments", "appendix", "results", "introduction", "discussion", "conclusion", "related_work", "theory", "references", "other"],
    "devils_advocate": ["abstract", "introduction", "method", "experiments", "results", "discussion", "conclusion", "theory", "related_work", "appendix", "references", "other"],
    "practitioner": ["abstract", "introduction", "method", "results", "experiments", "discussion", "conclusion", "appendix", "related_work", "theory", "references", "other"],
}


@dataclass
class ModelSpec:
    provider: str
    model: str
    base_url: str
    api_key_env: str | None
    context_window: int
    max_output_tokens: int
    json_mode: bool
    reasoning_effort: str | None = None
    price_input_per_mtok: float = 0.0
    price_output_per_mtok: float = 0.0

    def estimate_cost(self, tokens_in: int, tokens_out: int) -> float:
        return (
            tokens_in / 1_000_000.0 * self.price_input_per_mtok
            + tokens_out / 1_000_000.0 * self.price_output_per_mtok
        )


def _load_optional_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return read_json(path)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Cannot parse configuration file {path}: {exc}") from exc


def load_profiles() -> dict[str, Any]:
    data = _load_optional_json(CONFIG_DIR / "profiles.json")
    if data is None:
        return {"schema_version": SCHEMA_VERSION, "profiles": copy.deepcopy(DEFAULT_PROFILES), "decision_policy": copy.deepcopy(DEFAULT_DECISION_POLICY)}
    profiles = data.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise ConfigError("config/profiles.json has no 'profiles' object")
    return data


def load_models() -> dict[str, Any]:
    data = _load_optional_json(CONFIG_DIR / "models.json")
    if data is None:
        return copy.deepcopy(DEFAULT_MODELS)
    if not isinstance(data.get("providers"), dict):
        raise ConfigError("config/models.json has no 'providers' object")
    return data


def get_profile(name: str) -> dict[str, Any]:
    profiles = load_profiles()["profiles"]
    if name not in profiles:
        raise ConfigError(f"Unknown profile '{name}'. Known profiles: {', '.join(sorted(profiles))}")
    return profiles[name]


def get_decision_policy() -> dict[str, Any]:
    data = load_profiles()
    policy = copy.deepcopy(DEFAULT_DECISION_POLICY)
    policy.update(data.get("decision_policy", {}))
    return policy


def resolve_model(
    provider: str | None = None,
    model: str | None = None,
    *,
    models_data: dict[str, Any] | None = None,
) -> ModelSpec:
    registry = models_data or load_models()
    provider_name = provider or registry.get("default_provider", "deepseek")
    providers = registry.get("providers", {})
    if provider_name not in providers:
        raise ConfigError(f"Unknown provider '{provider_name}'. Known providers: {', '.join(sorted(providers))}")
    entry = providers[provider_name]
    model_name = model or entry.get("default_model")
    models = entry.get("models", {})
    if model_name not in models:
        raise ConfigError(f"Unknown model '{model_name}' for provider '{provider_name}'")
    spec = models[model_name]
    price = spec.get("price_per_mtok", {})
    return ModelSpec(
        provider=provider_name,
        model=model_name,
        base_url=str(entry.get("base_url", "")).rstrip("/"),
        api_key_env=entry.get("api_key_env"),
        context_window=int(spec.get("context_window", 128000)),
        max_output_tokens=int(spec.get("max_output_tokens", 8192)),
        json_mode=bool(spec.get("json_mode", False)),
        reasoning_effort=spec.get("reasoning_effort"),
        price_input_per_mtok=float(price.get("input", 0.0)),
        price_output_per_mtok=float(price.get("output", 0.0)),
    )


def read_api_key(api_key_env: str | None, env_file: str | None = None) -> str | None:
    if not api_key_env:
        return None
    value = os.environ.get(api_key_env)
    if value:
        return value
    if env_file:
        path = Path(env_file).expanduser()
        if path.is_file():
            for raw in path.read_text(encoding="utf-8-sig").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                if key.strip() == api_key_env:
                    return val.strip().strip('"').strip("'") or None
    default_env = Path.home() / ".hpe-paper-evaluator" / ".env"
    if default_env.is_file():
        return read_api_key(api_key_env, str(default_env))
    return None


def personas() -> list[dict[str, Any]]:
    path = PROMPTS_DIR / "personas.json"
    data = _load_optional_json(path)
    if not data:
        return [{"id": pid, "name": pid, "prompt_card": ""} for pid in PERSONA_IDS]
    return list(data.get("personas", []))


def get_persona(persona_id: str) -> dict[str, Any]:
    for persona in personas():
        if persona.get("id") == persona_id:
            return persona
    raise ConfigError(f"Unknown persona '{persona_id}'. Known personas: {', '.join(PERSONA_IDS)}")


def section_priority(persona_id: str) -> list[str]:
    return SECTION_PRIORITY.get(persona_id, SECTION_PRIORITY["methodology"])


@dataclass
class ContextLimits:
    max_paper_tokens: int = 60000
    max_output_tokens_per_call: int = 8000
    max_prior_tokens: int = 12000
    max_digest_tokens: int = 1800
    allow_truncation: bool = False
    safety_margin: int = 2000
    include_digest: bool = True
    include_references: bool = False
    max_reference_entries: int = 20


@dataclass
class Budget:
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_cost_usd: float | None = None
    max_wall_seconds: int | None = None
    spent_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0

    def can_spend(self, estimated_cost: float) -> bool:
        if self.max_cost_usd is not None and self.spent_usd + estimated_cost > self.max_cost_usd:
            return False
        return True

    def record(self, tokens_in: int, tokens_out: int, cost_usd: float) -> None:
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        self.spent_usd += cost_usd


@dataclass
class RunOptions:
    profile: str = "conference"
    mode: str = "host"
    provider: str | None = None
    model: str | None = None
    rounds: int = 2
    language: str = "auto"
    reviewers: list[dict[str, Any]] = field(default_factory=list)
    author_mode: str = "simulated"
    author_id: str = "A1"
    author_provider: str | None = None
    author_model: str | None = None
    provided_rebuttal: str | None = None
    purpose: str = "review"
    material: str = "paper"
    domain: str = "ai-ict"
    summary_style: str = "upstream"
    llm_config: str | None = None
    agent_backend: str | None = None
    allow_backend_mismatch: bool = False
    digest_backend: str | None = None
    reviewer_backends: list[str] = field(default_factory=list)
    author_backend: str | None = None
    meta_backend: str | None = None
    isolation: str = "subagent"
    vision_pages: str = "auto"
    vision_max_pages: int = 20
    vision_dpi: int = 150
    blind_paper: bool = False
    strict_evidence: bool = True
    early_stop: bool = True
    allow_poor_extraction: bool = False
    ocr: bool = False
    digest: bool = True
    prompt_set: str = PROMPT_VERSION
    context: ContextLimits = field(default_factory=ContextLimits)
    budget: Budget = field(default_factory=Budget)
