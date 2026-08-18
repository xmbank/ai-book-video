from __future__ import annotations

import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from book_video_workbench.util import (
    public_error_message,
    read_json,
    sha256_file,
    utc_now,
    write_json,
)


LEGACY_STAGES = [
    "source", "transcript", "rewrite", "tts", "subtitles", "storyboard", "render", "validate"
]

STAGES = [
    "repair",
    "book_info",
    "rewrite",
    "audio",
    "scene_images",
    "styles",
    "outputs",
    "review",
]

LEGACY_STAGE_MAP = {
    "repair": ("source", "transcript"),
    "book_info": (),
    "rewrite": ("rewrite",),
    "audio": ("tts", "subtitles"),
    "scene_images": (),
    "styles": ("storyboard",),
    "outputs": ("render", "validate"),
    "review": (),
}


class PipelineState:
    def __init__(self, task_dir: Path) -> None:
        self.task_dir = task_dir.resolve()
        self.path = self.task_dir / "pipeline-state.json"
        self.artifacts_path = self.task_dir / "artifacts.json"
        if self.path.exists():
            self.data = read_json(self.path)
            self._migrate_legacy_stages()
        else:
            self.data = {
                "schema_version": 3,
                "updated_at": utc_now(),
                "stages": {
                    name: {"status": "not_started", "artifacts": []}
                    for name in STAGES
                },
            }
            self.save()
        if not self.artifacts_path.exists():
            write_json(self.artifacts_path, {"schema_version": 1, "artifacts": []})

    def _migrate_legacy_stages(self) -> None:
        existing = self.data.get("stages") or {}
        if all(name in existing for name in STAGES):
            if list(existing) != STAGES or int(self.data.get("schema_version") or 1) < 3:
                self.data["stages"] = {name: existing[name] for name in STAGES}
                self.data["schema_version"] = 3
                self.save()
            return
        if not any(name in existing for name in LEGACY_STAGES):
            self.data["stages"] = {
                name: existing.get(name, {"status": "not_started", "artifacts": []})
                for name in STAGES
            }
            return
        self.data["legacy_stages"] = existing
        migrated: dict[str, dict] = {}
        for name in STAGES:
            sources = [existing[item] for item in LEGACY_STAGE_MAP[name] if item in existing]
            if not sources:
                status = "stale" if name in {"book_info", "scene_images", "styles", "outputs", "review"} else "not_started"
                migrated[name] = {
                    "status": status,
                    "artifacts": [],
                    "message": "旧任务需要按新图书带货流程补做",
                }
                continue
            statuses = [item.get("status", "not_started") for item in sources]
            status = "succeeded" if all(value == "succeeded" for value in statuses) else statuses[-1]
            migrated[name] = {
                "status": status,
                "artifacts": [path for item in sources for path in item.get("artifacts", [])],
                "message": "由旧流程迁移",
            }
        self.data["stages"] = migrated
        self.data["schema_version"] = 3
        self.save()

    def save(self) -> None:
        self.data["updated_at"] = utc_now()
        write_json(self.path, self.data)

    def stage_complete(self, name: str) -> bool:
        record = self.data["stages"][name]
        if record.get("status") != "succeeded":
            return False
        artifacts = record.get("artifacts") or []
        return bool(artifacts) and all((self.task_dir / item).exists() for item in artifacts)

    def invalidate_from(self, name: str) -> None:
        start = STAGES.index(name)
        for stage_name in STAGES[start:]:
            stage = self.data["stages"][stage_name]
            if stage.get("status") == "succeeded":
                stage["status"] = "stale"
                stage["message"] = f"由 {name} 强制重跑导致过期"
        self.save()

    @contextmanager
    def running(self, name: str) -> Iterator[list[Path]]:
        stage = self.data["stages"][name]
        stage.update(
            {
                "status": "running",
                "started_at": utc_now(),
                "ended_at": None,
                "message": "处理中",
                "error": None,
            }
        )
        self.save()
        outputs: list[Path] = []
        try:
            yield outputs
        except Exception as exc:
            public_message, retryable = public_error_message(exc, stage=name)
            stage.update(
                {
                    "status": "failed",
                    "ended_at": utc_now(),
                    "message": public_message,
                    "error": {
                        "type": type(exc).__name__,
                        "message": public_message,
                        "retryable": retryable,
                        "technical_message": str(exc),
                        "traceback": traceback.format_exc(),
                    },
                }
            )
            self.save()
            raise
        else:
            relative = [str(path.resolve().relative_to(self.task_dir)) for path in outputs]
            stage.update(
                {
                    "status": "succeeded",
                    "ended_at": utc_now(),
                    "message": "完成",
                    "artifacts": relative,
                    "error": None,
                }
            )
            self._record_artifacts(name, outputs)
            self.save()

    def _record_artifacts(self, stage: str, paths: list[Path]) -> None:
        index = read_json(self.artifacts_path)
        existing = {
            (item["stage"], item["path"]): item for item in index["artifacts"]
        }
        for path in paths:
            resolved = path.resolve()
            relative = str(resolved.relative_to(self.task_dir))
            if not resolved.is_file():
                continue
            existing[(stage, relative)] = {
                "stage": stage,
                "path": relative,
                "size_bytes": resolved.stat().st_size,
                "sha256": sha256_file(resolved),
                "recorded_at": utc_now(),
            }
        index["artifacts"] = list(existing.values())
        write_json(self.artifacts_path, index)
