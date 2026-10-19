"""Refresh (US-06): reload from EIA and rebuild the model, as a background job the Admin can follow.

POST /api/refresh starts a job and answers 202 at once; GET /api/refresh/{job_id} reports its progress
and outcome. One job runs at a time (and the connector's file lock also keeps out a CLI run). Jobs are
written to data/_meta/refresh_jobs/, so the outcome survives a restart; a job that was running when the
server stopped is reported as `interrupted`.

Outcome:
- succeeded: every dataset extracted, model rebuilt; the API serves the new build from the next request;
- partial:   some chunks failed (their old data is kept and their watermark stays, DECISIONS #12), the
             model was rebuilt from what is stored; the next refresh retries the failed window;
- failed:    nothing new is served (bad credentials, EIA down for every chunk, another run active,
             or the model build failed).
"""

import json
import logging
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from outage_explorer.api.auth import require
from outage_explorer.api.errors import ApiError
from outage_explorer.api.policy import Capability
from outage_explorer.api.users import User
from outage_explorer.config import Settings
from outage_explorer.connector.extract import meta_dir, run_extraction
from outage_explorer.model.build import build_model

log = logging.getLogger(__name__)

Status = Literal["running", "succeeded", "partial", "failed", "interrupted"]
FINISHED = {"succeeded", "partial", "failed", "interrupted"}
HISTORY = 20


class DatasetResult(BaseModel):
    status: str
    rows_fetched: int
    inserted: int
    updated: int
    watermark: str | None


class JobOut(BaseModel):
    job_id: str
    status: Status
    step: Literal["extract", "model", "done"]
    full: bool
    requested_by: str
    started_at: str
    finished_at: str | None = None
    extraction_run_id: str | None = None
    datasets: dict[str, DatasetResult] = {}
    build_id: str | None = None
    dq_issues: dict[str, int] = {}  # findings of the new build's checks, by severity
    error: str | None = None


class RefreshFailed(Exception):
    pass


class RefreshIn(BaseModel):
    full: bool = False  # re-extract everything from 2007 instead of from the watermarks


class RefreshJobs:
    def __init__(self, settings: Settings, on_new_build: Callable[[], None] = lambda: None):
        self.settings = settings
        self.on_new_build = on_new_build  # e.g. clear the query cache
        self.dir = meta_dir(settings.data_path) / "refresh_jobs"
        self._lock = threading.Lock()
        self._jobs: dict[str, JobOut] = self._load()

    def start(self, user: User, full: bool) -> JobOut:
        with self._lock:
            running = [j for j in self._jobs.values() if j.status == "running"]
            if running:
                raise ApiError(409, "refresh_in_progress", f"refresh {running[0].job_id} is still running")
            job = JobOut(
                job_id=datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6],
                status="running",
                step="extract",
                full=full,
                requested_by=user.username,
                started_at=datetime.now(UTC).isoformat(),
            )
            self._save(job)
        threading.Thread(
            target=self._run, args=(job.job_id,), name=f"refresh-{job.job_id}", daemon=True
        ).start()
        return job

    def get(self, job_id: str) -> JobOut:
        with self._lock:
            if job_id not in self._jobs:
                raise ApiError(404, "job_not_found", f"no refresh job '{job_id}'")
            return self._jobs[job_id].model_copy()

    def recent(self) -> list[JobOut]:
        with self._lock:
            return [j.model_copy() for j in sorted(self._jobs.values(), key=lambda j: j.job_id, reverse=True)]

    def _run(self, job_id: str) -> None:
        job = self.get(job_id)
        try:
            summary = run_extraction(self.settings, full=job.full)
            job.extraction_run_id = summary["run_id"]
            job.datasets = {
                name: DatasetResult(**{k: ds.get(k) or 0 for k in ("rows_fetched", "inserted", "updated")},
                                    status=ds["status"], watermark=ds.get("watermark"))
                for name, ds in summary["datasets"].items()
            }  # fmt: skip
            job.error = _extraction_errors(summary)  # kept for a partial run, too
            if summary["status"] == "failed":
                raise RefreshFailed(f"extraction failed, nothing was rebuilt: {job.error}")
            job.step = "model"
            self._update(job)

            manifest = build_model(self.settings)
            job.build_id = manifest["build_id"]
            self.on_new_build()
            job.dq_issues = _by_severity(manifest.get("data_quality", {}))
            job.status = "succeeded" if summary["status"] == "success" else "partial"
        except RefreshFailed as exc:
            log.error("refresh %s failed: %s", job_id, exc)
            job.status, job.error = "failed", str(exc)
        except Exception as exc:  # the job reports every failure; nothing may escape the thread silently
            log.exception("refresh %s failed", job_id)
            job.status, job.error = "failed", f"{type(exc).__name__}: {exc}"
        job.step = "done"
        job.finished_at = datetime.now(UTC).isoformat()
        self._update(job)

    def _update(self, job: JobOut) -> None:
        with self._lock:
            self._save(job)

    def _save(self, job: JobOut) -> None:
        self._jobs[job.job_id] = job.model_copy()
        _write(self.dir / f"{job.job_id}.json", job)

    def _load(self) -> dict[str, JobOut]:
        jobs: dict[str, JobOut] = {}
        paths = sorted(self.dir.glob("*.json")) if self.dir.exists() else []
        for path in paths[-HISTORY:]:
            job = JobOut.model_validate(json.loads(path.read_text(encoding="utf-8")))
            if job.status == "running":  # the server stopped mid-job: its thread is gone
                job.status, job.step, job.error = (
                    "interrupted",
                    "done",
                    "the server stopped during this refresh",
                )
                _write(path, job)
            jobs[job.job_id] = job
        return jobs


def _write(path: Path, job: JobOut) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.stem}.tmp")
    tmp.write_text(job.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(path)  # atomic: a reader never sees half a job


def _extraction_errors(summary: dict, limit: int = 3) -> str | None:
    """Why the extraction was not a full success, in a few lines (EIA errors never contain the key)."""
    if summary.get("error"):
        return summary["error"]
    errors = [
        f"{name} {c['start']}..{c['end']}: {c['error']}"
        for name, ds in summary["datasets"].items()
        for c in ds.get("chunks", [])
        if c.get("error")
    ]
    if not errors:
        return None
    more = f" (+{len(errors) - limit} more)" if len(errors) > limit else ""
    return "; ".join(errors[:limit]) + more


def _by_severity(data_quality: dict[str, dict[str, int]]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for counts in data_quality.values():
        for severity, n in counts.items():
            totals[severity] = totals.get(severity, 0) + n
    return totals


router = APIRouter(prefix="/api/refresh", tags=["refresh"])
AdminUser = Annotated[User, Depends(require(Capability.REFRESH))]


@router.post("", status_code=202, response_model=JobOut)
def start_refresh(user: AdminUser, request: Request, body: RefreshIn | None = None) -> JobOut:
    return request.app.state.refresh_jobs.start(user, full=(body or RefreshIn()).full)


@router.get("", response_model=list[JobOut])
def list_refreshes(user: AdminUser, request: Request) -> list[JobOut]:
    return request.app.state.refresh_jobs.recent()


@router.get("/{job_id}", response_model=JobOut)
def get_refresh(job_id: str, user: AdminUser, request: Request) -> JobOut:
    return request.app.state.refresh_jobs.get(job_id)
