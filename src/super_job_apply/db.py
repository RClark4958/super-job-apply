"""SQLite database layer for job and application tracking."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from .models import Application, ApplicationStatus, JobPosting

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT '',
    company_name TEXT NOT NULL,
    job_title TEXT NOT NULL,
    careers_url TEXT NOT NULL,
    company_url TEXT,
    location TEXT,
    work_type TEXT,
    requirements TEXT NOT NULL DEFAULT '[]',
    responsibilities TEXT NOT NULL DEFAULT '[]',
    benefits TEXT NOT NULL DEFAULT '[]',
    full_description TEXT NOT NULL DEFAULT '',
    discovered_at TEXT NOT NULL,
    UNIQUE(company_name, job_title)
);

CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company_name);
CREATE INDEX IF NOT EXISTS idx_jobs_discovered ON jobs(discovered_at);

CREATE TABLE IF NOT EXISTS applications (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    status TEXT NOT NULL DEFAULT 'pending',
    match_score REAL,
    resume_path TEXT,
    cover_letter_path TEXT,
    session_url TEXT,
    error_message TEXT,
    applied_at TEXT,
    created_at TEXT NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_apps_status ON applications(status);
CREATE INDEX IF NOT EXISTS idx_apps_applied ON applications(applied_at);
"""


class Database:
    """Async SQLite database for tracking jobs and applications."""

    def __init__(self, db_path: str = "./applications.db"):
        self.db_path = db_path

    async def init(self) -> None:
        """Create tables if they don't exist."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(_SCHEMA)

    async def job_exists(self, company_name: str, job_title: str) -> bool:
        """Check if a job already exists (deduplication)."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT 1 FROM jobs WHERE company_name = ? AND job_title = ?",
                (company_name, job_title),
            )
            return await cursor.fetchone() is not None

    async def insert_job(self, job: JobPosting) -> str:
        """Insert a new job posting. Returns job id. Skips if duplicate."""
        async with aiosqlite.connect(self.db_path) as db:
            try:
                await db.execute(
                    """INSERT INTO jobs
                    (id, source, company_name, job_title, careers_url, company_url,
                     location, work_type, requirements, responsibilities, benefits,
                     full_description, discovered_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        job.id,
                        job.source,
                        job.company_name,
                        job.job_title,
                        job.careers_url,
                        job.company_url,
                        job.location,
                        job.work_type,
                        json.dumps(job.requirements),
                        json.dumps(job.responsibilities),
                        json.dumps(job.benefits),
                        job.full_description,
                        job.discovered_at.isoformat(),
                    ),
                )
                await db.commit()
                return job.id
            except aiosqlite.IntegrityError:
                # Duplicate — return existing id
                cursor = await db.execute(
                    "SELECT id FROM jobs WHERE company_name = ? AND job_title = ?",
                    (job.company_name, job.job_title),
                )
                row = await cursor.fetchone()
                return row[0] if row else job.id

    async def get_job(self, job_id: str) -> JobPosting | None:
        """Fetch a job by id."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            return _row_to_job(row)

    async def insert_application(self, app: Application) -> str:
        """Insert a new application record."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO applications
                (id, job_id, status, match_score, resume_path, cover_letter_path,
                 session_url, error_message, applied_at, created_at, retry_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    app.id,
                    app.job_id,
                    app.status.value,
                    app.match_score,
                    app.resume_path,
                    app.cover_letter_path,
                    app.session_url,
                    app.error_message,
                    app.applied_at.isoformat() if app.applied_at else None,
                    app.created_at.isoformat(),
                    app.retry_count,
                ),
            )
            await db.commit()
            return app.id

    async def update_application(self, app_id: str, **kwargs) -> None:
        """Update application fields by id."""
        if not kwargs:
            return
        # Convert enums and datetimes
        updates = {}
        for k, v in kwargs.items():
            if isinstance(v, ApplicationStatus):
                updates[k] = v.value
            elif isinstance(v, datetime):
                updates[k] = v.isoformat()
            else:
                updates[k] = v

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [app_id]

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f"UPDATE applications SET {set_clause} WHERE id = ?",  # noqa: S608
                values,
            )
            await db.commit()

    async def get_applications(
        self,
        status: ApplicationStatus | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Query applications with optional status filter, joined with job data."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            query = """
                SELECT a.*, j.company_name, j.job_title, j.careers_url,
                       j.full_description, j.requirements, j.responsibilities,
                       j.location, j.work_type
                FROM applications a
                JOIN jobs j ON a.job_id = j.id
            """
            params: list = []
            if status:
                query += " WHERE a.status = ?"
                params.append(status.value)
            query += " ORDER BY a.created_at DESC LIMIT ?"
            params.append(limit)

            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_stats(self) -> dict:
        """Get aggregated application statistics."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            # Total jobs discovered
            cursor = await db.execute("SELECT COUNT(*) as count FROM jobs")
            jobs_total = (await cursor.fetchone())["count"]

            # Applications by status
            cursor = await db.execute(
                "SELECT status, COUNT(*) as count FROM applications GROUP BY status"
            )
            status_counts = {row["status"]: row["count"] for row in await cursor.fetchall()}

            # Total applications
            total_apps = sum(status_counts.values())

            # Average match score for applied
            cursor = await db.execute(
                "SELECT AVG(match_score) as avg_score FROM applications WHERE match_score IS NOT NULL"
            )
            row = await cursor.fetchone()
            avg_score = row["avg_score"] if row and row["avg_score"] else 0.0

            return {
                "jobs_discovered": jobs_total,
                "total_applications": total_apps,
                "by_status": status_counts,
                "avg_match_score": round(avg_score, 3),
            }

    async def has_application_for_job(self, job_id: str) -> bool:
        """Check if an application already exists for this job."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT 1 FROM applications WHERE job_id = ?", (job_id,)
            )
            return await cursor.fetchone() is not None


def _row_to_job(row) -> JobPosting:
    """Convert a database row to a JobPosting model."""
    return JobPosting(
        id=row["id"],
        source=row["source"],
        company_name=row["company_name"],
        job_title=row["job_title"],
        careers_url=row["careers_url"],
        company_url=row["company_url"],
        location=row["location"],
        work_type=row["work_type"],
        requirements=json.loads(row["requirements"]),
        responsibilities=json.loads(row["responsibilities"]),
        benefits=json.loads(row["benefits"]),
        full_description=row["full_description"],
        discovered_at=datetime.fromisoformat(row["discovered_at"]),
    )
