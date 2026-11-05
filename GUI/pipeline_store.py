from __future__ import annotations
from GUI.project_paths import json_dump as _pp_json_dump, json_load as _pp_json_load

import json
import math
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


PIPELINE_FILE_VERSION = 1


def utc_now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex


def json_value(value: Any) -> Any:
    """Convert common parameter values to stable JSON-compatible values."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_value(item) for item in value]

    # NumPy scalars and similar objects usually expose item().
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return json_value(item())
        except Exception:
            pass
    return str(value)


def _safe_filename(value: str) -> str:
    value = re.sub(r'[\\/:*?"<>|]+', "_", str(value or "").strip())
    value = re.sub(r"\s+", "_", value).strip("._")
    return value or "pipeline"


class PipelineStore:
    """Persistent named pipelines for one project tab."""

    def __init__(self, project_root: Any, pipeline_type: str):
        self.project_root = Path(project_root)
        self.pipeline_type = str(pipeline_type).strip().lower()
        if self.pipeline_type not in {"processing", "analysis"}:
            raise ValueError(f"Unsupported pipeline type: {pipeline_type}")
        self.directory = self.project_root / "Pipelines"
        self.path = self.directory / f"{self.pipeline_type}_pipelines.json"
        self.data = self._load()
        if self._mark_interrupted_runs():
            try:
                self.save()
            except Exception:
                pass

    def _empty_data(self) -> dict:
        return {
            "version": PIPELINE_FILE_VERSION,
            "pipeline_type": self.pipeline_type,
            "pipelines": [],
        }

    def _load(self) -> dict:
        if not self.path.exists():
            return self._empty_data()
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                data = _pp_json_load(stream)
        except Exception:
            # Keep a corrupt file untouched. The next successful save is written
            # atomically and will replace it only after valid JSON is ready.
            return self._empty_data()
        if not isinstance(data, dict):
            return self._empty_data()
        pipelines = data.get("pipelines", [])
        if not isinstance(pipelines, list):
            pipelines = []
        data["version"] = PIPELINE_FILE_VERSION
        data["pipeline_type"] = self.pipeline_type
        data["pipelines"] = pipelines
        return data

    def save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = json_value(self.data)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            _pp_json_dump(payload, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        temporary.replace(self.path)

    def _mark_interrupted_runs(self) -> bool:
        """Convert a stale running state left by a closed application."""
        changed = False
        now = utc_now_text()
        for pipeline in self.data.get("pipelines", []):
            pipeline_run = pipeline.get("last_run") or {}
            if str(pipeline_run.get("status", "")) != "running":
                continue
            changed = True
            failed_steps = int(pipeline_run.get("failed_steps", 0) or 0)
            for step in pipeline.get("steps", []) or []:
                step_run = step.get("last_run") or {}
                if str(step_run.get("status", "")) == "running":
                    step["last_run"] = {
                        "status": "failed",
                        "started_at": step_run.get("started_at"),
                        "finished_at": now,
                        "error": "The application closed before this pipeline step completed.",
                        "log": str(step_run.get("log", "") or ""),
                    }
                    failed_steps += 1
                elif str(step_run.get("status", "")) == "pending":
                    step["last_run"] = {
                        "status": "not_run",
                        "error": "The pipeline was interrupted before this step started.",
                        "log": "",
                    }
            pipeline["last_run"] = {
                **pipeline_run,
                "status": "interrupted",
                "finished_at": now,
                "failed_steps": failed_steps,
            }
            pipeline["updated_at"] = now
        return changed

    def pipelines(self) -> list[dict]:
        return list(self.data.get("pipelines", []))

    def get(self, pipeline_id: Optional[str]) -> Optional[dict]:
        wanted = str(pipeline_id or "")
        for pipeline in self.data.get("pipelines", []):
            if str(pipeline.get("id", "")) == wanted:
                return pipeline
        return None

    def find_by_name(self, name: str) -> Optional[dict]:
        wanted = str(name or "").strip().casefold()
        for pipeline in self.data.get("pipelines", []):
            if str(pipeline.get("name", "")).strip().casefold() == wanted:
                return pipeline
        return None

    def create(self, name: str) -> dict:
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("Pipeline name is required.")
        if self.find_by_name(clean_name) is not None:
            raise ValueError(f'A pipeline named "{clean_name}" already exists.')
        now = utc_now_text()
        pipeline = {
            "id": new_id(),
            "name": clean_name,
            "steps": [],
            "created_at": now,
            "updated_at": now,
            "last_run": None,
        }
        self.data.setdefault("pipelines", []).append(pipeline)
        self.save()
        return pipeline

    def rename(self, pipeline_id: str, name: str) -> dict:
        pipeline = self.get(pipeline_id)
        if pipeline is None:
            raise KeyError("Pipeline not found.")
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("Pipeline name is required.")
        existing = self.find_by_name(clean_name)
        if existing is not None and existing is not pipeline:
            raise ValueError(f'A pipeline named "{clean_name}" already exists.')
        pipeline["name"] = clean_name
        pipeline["updated_at"] = utc_now_text()
        self.save()
        return pipeline

    def delete(self, pipeline_id: str) -> bool:
        pipelines = self.data.setdefault("pipelines", [])
        original_count = len(pipelines)
        pipelines[:] = [p for p in pipelines if str(p.get("id", "")) != str(pipeline_id)]
        changed = len(pipelines) != original_count
        if changed:
            self.save()
        return changed

    def add_step(self, pipeline_id: str, name: str, snapshot: dict) -> dict:
        pipeline = self.get(pipeline_id)
        if pipeline is None:
            raise KeyError("Pipeline not found.")
        step = {
            "id": new_id(),
            "name": str(name or "Step"),
            "snapshot": json_value(snapshot),
            "last_run": None,
        }
        pipeline.setdefault("steps", []).append(step)
        pipeline["updated_at"] = utc_now_text()
        self.save()
        return step

    def update_step(self, pipeline_id: str, step_id: str, name: str, snapshot: dict) -> dict:
        pipeline = self.get(pipeline_id)
        if pipeline is None:
            raise KeyError("Pipeline not found.")
        for step in pipeline.setdefault("steps", []):
            if str(step.get("id", "")) == str(step_id):
                step["name"] = str(name or "Step")
                step["snapshot"] = json_value(snapshot)
                step["last_run"] = None
                pipeline["updated_at"] = utc_now_text()
                self.save()
                return step
        raise KeyError("Pipeline step not found.")

    def rename_step(self, pipeline_id: str, step_id: str, name: str) -> dict:
        """Rename a step without changing its snapshot or previous run result."""
        pipeline = self.get(pipeline_id)
        if pipeline is None:
            raise KeyError("Pipeline not found.")
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("Pipeline step name is required.")
        for step in pipeline.setdefault("steps", []):
            if str(step.get("id", "")) == str(step_id):
                step["name"] = clean_name
                pipeline["updated_at"] = utc_now_text()
                self.save()
                return step
        raise KeyError("Pipeline step not found.")

    def remove_step(self, pipeline_id: str, step_id: str) -> bool:
        pipeline = self.get(pipeline_id)
        if pipeline is None:
            return False
        steps = pipeline.setdefault("steps", [])
        original_count = len(steps)
        steps[:] = [step for step in steps if str(step.get("id", "")) != str(step_id)]
        changed = len(steps) != original_count
        if changed:
            pipeline["updated_at"] = utc_now_text()
            self.save()
        return changed

    def move_step(self, pipeline_id: str, step_id: str, offset: int) -> bool:
        pipeline = self.get(pipeline_id)
        if pipeline is None:
            return False
        steps = pipeline.setdefault("steps", [])
        index = next((i for i, step in enumerate(steps) if str(step.get("id", "")) == str(step_id)), -1)
        new_index = index + int(offset)
        if index < 0 or new_index < 0 or new_index >= len(steps):
            return False
        steps[index], steps[new_index] = steps[new_index], steps[index]
        pipeline["updated_at"] = utc_now_text()
        self.save()
        return True

    def write_report(self, pipeline: dict, lines: list[str], started_at: str) -> Path:
        report_directory = self.directory / "reports" / self.pipeline_type
        report_directory.mkdir(parents=True, exist_ok=True)
        try:
            timestamp = datetime.fromisoformat(str(started_at)).strftime("%Y%m%d_%H%M%S")
        except Exception:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = _safe_filename(str(pipeline.get("name", "pipeline")))
        path = report_directory / f"{name}_{timestamp}.txt"
        suffix = 2
        while path.exists():
            path = report_directory / f"{name}_{timestamp}_{suffix}.txt"
            suffix += 1
        path.write_text("\n".join(str(line) for line in lines).rstrip() + "\n", encoding="utf-8")
        return path

    def project_relative_path(self, path: Any) -> str:
        try:
            return str(Path(path).resolve().relative_to(self.project_root.resolve()))
        except Exception:
            return str(path)