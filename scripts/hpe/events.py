"""Append-only event log and usage accounting."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import EVENT_LOG, USAGE_FILE
from .util import append_jsonl, atomic_write_json, read_json, utc_now_iso


class EventLog:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.path = run_dir / EVENT_LOG

    def emit(self, event: str, **fields: Any) -> None:
        record = {"ts": utc_now_iso(), "event": event}
        record.update({key: value for key, value in fields.items() if value is not None})
        append_jsonl(self.path, record)

    def task_completed(self, *, task_id: str, stage: str, actor: str, status: str = "ok", **fields: Any) -> None:
        self.emit("task.completed", task_id=task_id, stage=stage, actor=actor, status=status, **fields)

    def task_failed(self, *, task_id: str, stage: str, actor: str, reason: str, **fields: Any) -> None:
        self.emit("task.failed", task_id=task_id, stage=stage, actor=actor, reason=reason, **fields)

    def warning(self, *, code: str, message: str, **fields: Any) -> None:
        self.emit("warning", code=code, message=message, **fields)

    def stage_changed(self, *, old: str, new: str, round: int, **fields: Any) -> None:
        self.emit("stage.changed", old=old, new=new, round=round, **fields)

    def validation_failed(self, *, task_id: str, errors: list[dict[str, Any]], **fields: Any) -> None:
        self.emit("validation.failed", task_id=task_id, errors=errors, **fields)


class UsageTracker:
    """Tracks usage honestly.

    Host mode often cannot know token counts. In that case ``known`` stays
    false and cost is not fabricated; API mode records real usage.
    """

    def __init__(self, run_dir: Path, *, max_cost_usd: float | None = None) -> None:
        self.run_dir = run_dir
        self.path = run_dir / USAGE_FILE
        self.max_cost_usd = max_cost_usd
        if self.path.is_file():
            try:
                self.data = read_json(self.path)
            except (OSError, ValueError):
                self.data = self._empty()
        else:
            self.data = self._empty()

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "schema_version": "hpe/v2",
            "known": False,
            "totals": {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0},
            "by_model": {},
            "by_stage": {},
        }

    def record(
        self,
        *,
        model: str,
        stage: str,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float,
    ) -> None:
        totals = self.data.setdefault("totals", {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0})
        totals["calls"] += 1
        totals["tokens_in"] += int(tokens_in)
        totals["tokens_out"] += int(tokens_out)
        totals["cost_usd"] = round(float(totals["cost_usd"]) + float(cost_usd), 6)
        self.data["known"] = True
        by_model = self.data.setdefault("by_model", {})
        model_row = by_model.setdefault(model, {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0})
        model_row["calls"] += 1
        model_row["tokens_in"] += int(tokens_in)
        model_row["tokens_out"] += int(tokens_out)
        model_row["cost_usd"] = round(float(model_row["cost_usd"]) + float(cost_usd), 6)
        by_stage = self.data.setdefault("by_stage", {})
        stage_row = by_stage.setdefault(stage, {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0})
        stage_row["calls"] += 1
        stage_row["tokens_in"] += int(tokens_in)
        stage_row["tokens_out"] += int(tokens_out)
        stage_row["cost_usd"] = round(float(stage_row["cost_usd"]) + float(cost_usd), 6)
        atomic_write_json(self.path, self.data)

    def record_host_usage(self, *, model: str, stage: str, tokens_in: int, tokens_out: int, cost_usd: float = 0.0) -> None:
        self.record(model=model, stage=stage, tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost_usd)

    @property
    def totals(self) -> dict[str, Any]:
        return dict(self.data.get("totals", {}))

    @property
    def known(self) -> bool:
        return bool(self.data.get("known", False))

    def budget_exceeded(self) -> bool:
        if self.max_cost_usd is None:
            return False
        return float(self.totals.get("cost_usd", 0.0)) > float(self.max_cost_usd)
