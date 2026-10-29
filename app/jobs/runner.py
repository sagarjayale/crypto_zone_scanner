"""Background jobs, each recorded in the jobs table so the pages can show progress and errors."""
import asyncio

from ..data.sync import now_utc
from ..db import SessionLocal
from ..models import Job


async def run_job(job_type: str, work, job_id: int | None = None) -> int:
    """Run `work(db, report)` and record the outcome. Never raises: a failure is stored on the job row."""
    if job_id is None:
        with SessionLocal() as db:
            job = Job(type=job_type, status="running", started_at=now_utc())
            db.add(job)
            db.commit()
            job_id = job.id

    def report(text: str):
        with SessionLocal() as d2:
            j = d2.get(Job, job_id)
            j.progress = text[:255]
            d2.commit()

    status, error = "done", None
    with SessionLocal() as db:
        try:
            await work(db, report)
        except Exception as exc:
            db.rollback()
            status, error = "failed", f"{type(exc).__name__}: {exc}"
    with SessionLocal() as d2:
        j = d2.get(Job, job_id)
        j.status, j.error, j.ended_at = status, error, now_utc()
        d2.commit()
    return job_id


def start_job(job_type: str, work) -> int:
    """Start a job without waiting for it. Returns the job id for progress polling."""
    with SessionLocal() as db:
        job = Job(type=job_type, status="running", started_at=now_utc())
        db.add(job)
        db.commit()
        job_id = job.id
    asyncio.get_running_loop().create_task(run_job(job_type, work, job_id))
    return job_id
