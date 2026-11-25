"""Typed errors and exit codes for the HPE v2 CLI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# Exit codes documented in SKILL.md / hpe-skill-v2.md.
EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_CREDENTIALS = 3
EXIT_VALIDATION = 4
EXIT_PROVIDER = 5
EXIT_BUDGET = 6
EXIT_STATE = 7
EXIT_INGEST = 8
EXIT_CANCELLED = 9


class HpeError(Exception):
    """Base error carrying a stable exit code and optional structured detail."""

    exit_code: int = 1
    code: str = "hpe.error"

    def __init__(self, message: str, *, detail: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.detail is not None:
            data["detail"] = self.detail
        return data


class ConfigError(HpeError):
    exit_code = EXIT_CONFIG
    code = "hpe.config"


class CredentialError(HpeError):
    exit_code = EXIT_CREDENTIALS
    code = "hpe.credentials"


class ValidationError(HpeError):
    exit_code = EXIT_VALIDATION
    code = "hpe.validation"


class ProviderError(HpeError):
    exit_code = EXIT_PROVIDER
    code = "hpe.provider"


class CoverageError(HpeError):
    exit_code = EXIT_PROVIDER
    code = "hpe.coverage"


class BudgetExceeded(HpeError):
    exit_code = EXIT_BUDGET
    code = "hpe.budget"


class StateError(HpeError):
    exit_code = EXIT_STATE
    code = "hpe.state"


class IngestError(HpeError):
    exit_code = EXIT_INGEST
    code = "hpe.ingest"


class Cancelled(HpeError):
    exit_code = EXIT_CANCELLED
    code = "hpe.cancelled"


@dataclass
class ValidationProblem:
    """One validation finding.

    ``level="error"`` blocks submission; ``warning`` is recorded in the run and
    rendered into the final report.
    """

    where: str
    message: str
    level: str = "error"
    code: str = "invalid"

    def to_dict(self) -> dict[str, str]:
        return {
            "level": self.level,
            "code": self.code,
            "where": self.where,
            "message": self.message,
        }


@dataclass
class ValidationResult:
    problems: list[ValidationProblem] = field(default_factory=list)
    sanitized: Any = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def errors(self) -> list[ValidationProblem]:
        return [p for p in self.problems if p.level == "error"]

    @property
    def warnings(self) -> list[ValidationProblem]:
        return [p for p in self.problems if p.level != "error"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def add(self, where: str, message: str, *, level: str = "error", code: str = "invalid") -> None:
        self.problems.append(ValidationProblem(where=where, message=message, level=level, code=code))

    def error(self, where: str, message: str, code: str = "invalid") -> None:
        self.add(where, message, level="error", code=code)

    def warning(self, where: str, message: str, code: str = "warning") -> None:
        self.add(where, message, level="warning", code=code)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": [p.to_dict() for p in self.errors],
            "warnings": [p.to_dict() for p in self.warnings],
            "extra": self.extra,
        }
