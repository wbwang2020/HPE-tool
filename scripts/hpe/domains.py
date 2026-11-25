"""Domain registry and domain cards.

The default domain is AI/CS/ICT. Materials (metals/high-entropy alloys) and
mechanical engineering have separate prompt cards. A user can add a domain by
dropping a Markdown card into ``prompts/domains/`` and registering it in
``config/domains.json``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import CONFIG_DIR, PROMPTS_DIR
from .errors import ConfigError
from .util import read_json


DEFAULT_DOMAINS: dict[str, dict[str, Any]] = {
    "ai-ict": {
        "label": "AI / CS / ICT",
        "prompt": "prompts/domains/ai-ict.md",
        "default_reviewers": ["methodology", "experiments", "novelty"],
        "profiles": ["conference", "journal", "paper", "proposal", "experiment"],
        "keywords": ["deep learning", "machine learning", "neural", "llm", "transformer", "reinforcement learning", "computer vision", "nlp", "dataset", "benchmark", "algorithm", "人工智能", "机器学习", "大模型", "计算机", "通信", "网络", "芯片", "ai"],
    },
    "materials": {
        "label": "金属材料 / 高熵合金",
        "prompt": "prompts/domains/materials-metal-hea.md",
        "default_reviewers": ["methodology", "experiments", "reproducibility"],
        "profiles": ["journal", "proposal", "experiment", "paper"],
        "keywords": ["alloy", "high-entropy", "high entropy", "hea", "metal", "steel", "titanium", "aluminum", "microstructure", "phase", "xrd", "sem", "tem", "ebsd", "tensile", "hardness", "corrosion", "fatigue", "thermodynamic", "高熵合金", "合金", "金属", "钢", "钛", "铝", "显微组织", "相", "拉伸", "硬度", "腐蚀", "疲劳"],
    },
    "mechanical": {
        "label": "机械工程",
        "prompt": "prompts/domains/mechanical.md",
        "default_reviewers": ["methodology", "experiments", "reproducibility"],
        "profiles": ["journal", "proposal", "experiment", "paper"],
        "keywords": ["mechanical", "structural", "fatigue", "fracture", "fea", "finite element", "vibration", "tribology", "manufacturing", "machining", "tolerance", "load", "stress", "strain", "机械", "结构", "疲劳", "断裂", "有限元", "振动", "摩擦", "制造", "加工", "公差", "载荷", "应力", "应变"],
    },
}


@dataclass
class DomainSpec:
    id: str
    label: str
    prompt_path: Path
    default_reviewers: list[str]
    profiles: list[str]
    keywords: list[str]
    data: dict[str, Any]

    def card(self) -> str:
        if not self.prompt_path.is_file():
            raise ConfigError(f"Domain prompt file missing: {self.prompt_path}")
        return self.prompt_path.read_text(encoding="utf-8").strip()


def load_domains() -> dict[str, dict[str, Any]]:
    path = CONFIG_DIR / "domains.json"
    if not path.is_file():
        return {key: dict(value, id=key) for key, value in DEFAULT_DOMAINS.items()}
    try:
        data = read_json(path)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Cannot read {path}: {exc}") from exc
    domains = data.get("domains")
    if not isinstance(domains, dict) or not domains:
        raise ConfigError(f"{path} has no 'domains' object")
    merged: dict[str, dict[str, Any]] = {}
    for key, value in domains.items():
        default = dict(DEFAULT_DOMAINS.get(key, {}))
        default.update(value or {})
        default["id"] = key
        merged[key] = default
    return merged


def get_domain(domain_id: str) -> DomainSpec:
    domains = load_domains()
    if domain_id not in domains:
        raise ConfigError(f"Unknown domain {domain_id!r}. Known: {', '.join(sorted(domains))}")
    data = domains[domain_id]
    prompt = str(data.get("prompt") or f"prompts/domains/{domain_id}.md")
    prompt_path = Path(prompt)
    if not prompt_path.is_absolute():
        prompt_path = PROMPTS_DIR.parent / prompt
    return DomainSpec(
        id=domain_id,
        label=str(data.get("label") or domain_id),
        prompt_path=prompt_path,
        default_reviewers=list(data.get("default_reviewers") or []),
        profiles=list(data.get("profiles") or []),
        keywords=list(data.get("keywords") or []),
        data=data,
    )


def detect_domain(text: str) -> str:
    """Keyword-based auto detection; conservative and only used for --domain auto."""

    normalized = text.casefold()
    best_id = "ai-ict"
    best_score = 0
    for domain_id, data in load_domains().items():
        score = 0
        for keyword in data.get("keywords") or []:
            if keyword.casefold() in normalized:
                score += 1
        if score > best_score:
            best_id, best_score = domain_id, score
    return best_id


def domain_default_personas(domain_id: str) -> list[str]:
    spec = get_domain(domain_id)
    return spec.default_reviewers or ["methodology", "experiments", "novelty"]


def domain_card(domain_id: str) -> str:
    return get_domain(domain_id).card()
