"""Run state, task DAG, atomic persistence and a cross-process run lock."""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .errors import StateError
from .util import (
    atomic_write_json,
    ensure_dir,
    now_age_seconds,
    utc_now_iso,
)

SCHEMA_VERSION = "hpe/v2"
TERMINAL_STATUSES = {"done", "failed", "skipped"}
STALE_TASK_SECONDS = 2 * 60 * 60
STALE_LOCK_SECONDS = 2 * 60 * 60


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class RunLock:
    """A simple run.lock with stale detection.

    The lock serializes state mutations (next/submit/fail/advance/render) across
    processes. It is intentionally not a distributed lock; a shared filesystem
    with unreliable advisory locks should use one host process instead.
    """

    def __init__(self, run_dir: Path, *, force: bool = False, command: str = "") -> None:
        self.run_dir = run_dir
        self.path = run_dir / "run.lock"
        self.force = force
        self.command = command
        self._owned = False

    def _read_lock(self) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _is_stale(self, data: dict[str, Any] | None) -> bool:
        if data is None:
            return True
        age = now_age_seconds(data.get("created_at"))
        if age is not None and age > STALE_LOCK_SECONDS:
            return True
        pid = data.get("pid")
        if isinstance(pid, int) and not _pid_alive(pid):
            return True
        return False

    def acquire(self) -> None:
        ensure_dir(self.run_dir)
        if self.force and self.path.exists():
            self.path.unlink(missing_ok=True)
        for attempt in range(2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                data = self._read_lock()
                if attempt == 0 and self._is_stale(data):
                    try:
                        self.path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    continue
                owner = data or {}
                raise StateError(
                    f"Run is locked by another process (pid={owner.get('pid')}, host={owner.get('host')}, "
                    f"created={owner.get('created_at')}, command={owner.get('command')}). "
                    "Use --force-unlock only after confirming no other process is writing."
                )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "pid": os.getpid(),
                        "host": socket.gethostname(),
                        "created_at": utc_now_iso(),
                        "command": self.command,
                    },
                    handle,
                )
                handle.flush()
                os.fsync(handle.fileno())
            self._owned = True
            return
        raise StateError("Could not acquire the run lock after removing a stale lock.")

    def release(self) -> None:
        if self._owned:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass
            self._owned = False

    def __enter__(self) -> "RunLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


@dataclass
class Task:
    task_id: str
    kind: str
    stage: str
    round: int
    actor_id: str
    persona: str
    label: str
    output: str
    schema_name: str
    prompt_role: str
    executor: str = "host"
    purpose: str = "review"
    status: str = "pending"
    depends_on: list[str] = field(default_factory=list)
    optional_depends_on: list[str] = field(default_factory=list)
    prompt_path: str | None = None
    prompt_sha256: str | None = None
    error: str | None = None
    created_at: str = field(default_factory=utc_now_iso)
    claimed_at: str | None = None
    completed_at: str | None = None
    usage: dict[str, Any] | None = None

    def to_state(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "stage": self.stage,
            "round": self.round,
            "actor_id": self.actor_id,
            "persona": self.persona,
            "label": self.label,
            "output": self.output,
            "schema_name": self.schema_name,
            "prompt_role": self.prompt_role,
            "executor": self.executor,
            "purpose": self.purpose,
            "status": self.status,
            "depends_on": list(self.depends_on),
            "optional_depends_on": list(self.optional_depends_on),
            "prompt_path": self.prompt_path,
            "prompt_sha256": self.prompt_sha256,
            "error": self.error,
            "created_at": self.created_at,
            "claimed_at": self.claimed_at,
            "completed_at": self.completed_at,
            "usage": self.usage,
        }

    @classmethod
    def from_state(cls, data: dict[str, Any]) -> "Task":
        return cls(
            task_id=data["task_id"],
            kind=data["kind"],
            stage=data["stage"],
            round=int(data.get("round", 0)),
            actor_id=data.get("actor_id", ""),
            persona=data.get("persona", ""),
            label=data.get("label", ""),
            output=data["output"],
            schema_name=data.get("schema_name", ""),
            prompt_role=data.get("prompt_role", ""),
            executor=data.get("executor", "host"),
            purpose=data.get("purpose", "review"),
            status=data.get("status", "pending"),
            depends_on=list(data.get("depends_on", [])),
            optional_depends_on=list(data.get("optional_depends_on", [])),
            prompt_path=data.get("prompt_path"),
            prompt_sha256=data.get("prompt_sha256"),
            error=data.get("error"),
            created_at=data.get("created_at", utc_now_iso()),
            claimed_at=data.get("claimed_at"),
            completed_at=data.get("completed_at"),
            usage=data.get("usage"),
        )


@dataclass
class RunState:
    run_id: str
    stage: str = "created"
    round: int = 0
    max_rounds: int = 2
    task_seq: int = 0
    tasks: dict[str, Task] = field(default_factory=dict)
    coverage: dict[str, Any] = field(default_factory=dict)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    policy: dict[str, Any] | None = None
    stop_reason: str | None = None
    isolation: str = "subagent"
    updated_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "stage": self.stage,
            "round": self.round,
            "max_rounds": self.max_rounds,
            "task_seq": self.task_seq,
            "tasks": {task_id: task.to_state() for task_id, task in sorted(self.tasks.items())},
            "coverage": self.coverage,
            "warnings": self.warnings,
            "policy": self.policy,
            "stop_reason": self.stop_reason,
            "isolation": self.isolation,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunState":
        tasks = {key: Task.from_state(value) for key, value in data.get("tasks", {}).items()}
        return cls(
            run_id=data["run_id"],
            stage=data.get("stage", "created"),
            round=int(data.get("round", 0)),
            max_rounds=int(data.get("max_rounds", 2)),
            task_seq=int(data.get("task_seq", 0)),
            tasks=tasks,
            coverage=dict(data.get("coverage", {})),
            warnings=list(data.get("warnings", [])),
            policy=data.get("policy"),
            stop_reason=data.get("stop_reason"),
            isolation=data.get("isolation", "subagent"),
            updated_at=data.get("updated_at", utc_now_iso()),
        )

    # -- task helpers -----------------------------------------------------

    def next_task_id(self) -> str:
        self.task_seq += 1
        return f"T-{self.task_seq:04d}"

    def add_task(self, task: Task) -> Task:
        if task.task_id in self.tasks:
            raise StateError(f"Duplicate task id: {task.task_id}")
        self.tasks[task.task_id] = task
        return task

    def tasks_for(self, *, stage: str | None = None, round: int | None = None, kind: str | None = None) -> list[Task]:
        out = []
        for task in self.tasks.values():
            if stage is not None and task.stage != stage:
                continue
            if round is not None and task.round != round:
                continue
            if kind is not None and task.kind != kind:
                continue
            out.append(task)
        return sorted(out, key=lambda t: t.task_id)

    def task(self, task_id: str) -> Task:
        if task_id not in self.tasks:
            raise StateError(f"Unknown task: {task_id}")
        return self.tasks[task_id]

    def is_terminal(self, task_id: str) -> bool:
        task = self.task(task_id)
        return task.status in TERMINAL_STATUSES

    def is_ready(self, task: Task) -> bool:
        for dep_id in task.depends_on:
            if dep_id not in self.tasks:
                return False
            if self.tasks[dep_id].status not in {"done", "skipped"}:
                return False
        for dep_id in task.optional_depends_on:
            if dep_id not in self.tasks:
                return False
            if self.tasks[dep_id].status not in TERMINAL_STATUSES:
                return False
        return True

    def reclaim_stale(self, *, timeout_seconds: int = STALE_TASK_SECONDS) -> list[str]:
        reclaimed: list[str] = []
        for task in self.tasks.values():
            if task.status != "running":
                continue
            age = now_age_seconds(task.claimed_at)
            if age is None or age > timeout_seconds:
                task.status = "pending"
                task.claimed_at = None
                reclaimed.append(task.task_id)
        if reclaimed:
            self.add_warning("task.reclaimed", f"Reclaimed stale running tasks: {', '.join(reclaimed)}")
        return reclaimed

    def claim(self, task_id: str) -> Task:
        task = self.task(task_id)
        if task.status not in {"pending", "running"}:
            raise StateError(f"Task {task_id} is {task.status}, not claimable")
        task.status = "running"
        task.claimed_at = utc_now_iso()
        return task

    def mark_done(self, task_id: str, *, usage: dict[str, Any] | None = None) -> Task:
        task = self.task(task_id)
        task.status = "done"
        task.error = None
        task.completed_at = utc_now_iso()
        if usage is not None:
            task.usage = usage
        return task

    def mark_failed(self, task_id: str, reason: str) -> Task:
        task = self.task(task_id)
        task.status = "failed"
        task.error = reason
        task.completed_at = utc_now_iso()
        return task

    def add_warning(self, code: str, message: str, *, severity: str = "warning") -> None:
        for existing in self.warnings:
            if existing.get("code") == code and existing.get("message") == message:
                return
        self.warnings.append({"code": code, "message": message, "severity": severity})

    def update_coverage(self, expected_reviewers: int, *, round_number: int | None = None) -> None:
        coverage_round = self.round if round_number is None else round_number
        reviews = [t for t in self.tasks.values() if t.kind == "reviewer_report" and t.round == coverage_round]
        done = [t for t in reviews if t.status == "done"]
        failed = [t for t in reviews if t.status == "failed"]
        self.coverage = {
            "round": coverage_round,
            "reviewers_expected": expected_reviewers,
            "reviewers_done": len(done),
            "reviewers_failed": len(failed),
            "complete": len(done) == expected_reviewers,
            "ratio": round(len(done) / max(1, expected_reviewers), 3),
            "failed_reviewers": [t.actor_id for t in failed],
        }


def run_state_path(run_dir: Path) -> Path:
    return run_dir / "state.json"


def run_manifest_path(run_dir: Path) -> Path:
    return run_dir / "run.json"


def load_state(run_dir: Path) -> RunState:
    path = run_state_path(run_dir)
    if not path.is_file():
        raise StateError(f"state.json not found in {run_dir}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StateError(f"Cannot read state.json: {exc}") from exc
    if data.get("schema_version") != SCHEMA_VERSION:
        raise StateError(f"Unsupported state schema: {data.get('schema_version')!r}")
    return RunState.from_dict(data)


def save_state(run_dir: Path, state: RunState) -> None:
    state.updated_at = utc_now_iso()
    atomic_write_json(run_state_path(run_dir), state.to_dict())


def load_run(run_dir: Path) -> dict[str, Any]:
    path = run_manifest_path(run_dir)
    if not path.is_file():
        raise StateError(f"run.json not found in {run_dir}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StateError(f"Cannot read run.json: {exc}") from exc
    return data


def save_run(run_dir: Path, run: dict[str, Any]) -> None:
    atomic_write_json(run_manifest_path(run_dir), run)


def new_state(run_id: str, *, max_rounds: int, isolation: str = "subagent") -> RunState:
    return RunState(run_id=run_id, max_rounds=max_rounds, isolation=isolation)


def list_tasks(state: RunState) -> list[dict[str, Any]]:
    return [task.to_state() for task in sorted(state.tasks.values(), key=lambda t: t.task_id)]


def all_tasks_terminal(state: RunState, tasks: Iterable[Task]) -> bool:
    return all(task.status in TERMINAL_STATUSES for task in tasks)
