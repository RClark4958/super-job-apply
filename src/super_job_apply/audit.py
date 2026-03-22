"""Audit trail for application events.

Records every significant event in the pipeline — job listing content,
resume modifications (before/after), cover letter drafts, editor feedback,
and final versions — to both SQLite and structured JSON files for review.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

import aiosqlite
from pydantic import BaseModel, Field

from .models import _new_id, _utcnow

logger = logging.getLogger(__name__)


class AuditEventType(str, Enum):
    JOB_DISCOVERED = "job_discovered"
    JOB_SCORED = "job_scored"
    RESUME_ORIGINAL = "resume_original"
    RESUME_DRAFT = "resume_writer_draft"
    RESUME_EDITOR_REVIEW = "resume_editor_review"
    RESUME_MEDIATOR_FINAL = "resume_mediator_final"
    COVER_LETTER_DRAFT = "cover_letter_writer_draft"
    COVER_LETTER_EDITOR_REVIEW = "cover_letter_editor_review"
    COVER_LETTER_MEDIATOR_FINAL = "cover_letter_mediator_final"
    ACCOUNT_CREATED = "account_created"
    APPLICATION_SUBMITTED = "application_submitted"
    APPLICATION_FAILED = "application_failed"
    FORM_RESPONSES = "form_responses"


class AuditEvent(BaseModel):
    id: str = Field(default_factory=_new_id)
    application_id: str | None = None
    job_id: str
    event_type: AuditEventType
    content: str = ""
    metadata: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)


_AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    application_id TEXT,
    job_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_job ON audit_events(job_id);
CREATE INDEX IF NOT EXISTS idx_audit_app ON audit_events(application_id);
CREATE INDEX IF NOT EXISTS idx_audit_type ON audit_events(event_type);
"""


class AuditTrail:
    """Records and retrieves audit events for application review."""

    def __init__(self, db_path: str, output_dir: str = "./output"):
        self.db_path = db_path
        self.output_dir = Path(output_dir) / "audit"

    async def init(self) -> None:
        """Create audit table if it doesn't exist."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(_AUDIT_SCHEMA)

    async def record(self, event: AuditEvent) -> None:
        """Record an audit event to both DB and file."""
        # Write to DB
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO audit_events
                (id, application_id, job_id, event_type, content, metadata, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    event.id,
                    event.application_id,
                    event.job_id,
                    event.event_type.value,
                    event.content,
                    json.dumps(event.metadata),
                    event.created_at.isoformat(),
                ),
            )
            await db.commit()

        # Write to structured file for easy review
        await self._write_event_file(event)

        logger.debug(
            f"Audit: {event.event_type.value} for job {event.job_id}"
        )

    async def log_job_listing(
        self,
        job_id: str,
        company: str,
        title: str,
        full_description: str,
        careers_url: str,
        requirements: list[str],
        responsibilities: list[str],
    ) -> None:
        """Log the full content of a discovered job listing."""
        await self.record(AuditEvent(
            job_id=job_id,
            event_type=AuditEventType.JOB_DISCOVERED,
            content=full_description,
            metadata={
                "company": company,
                "title": title,
                "careers_url": careers_url,
                "requirements": requirements,
                "responsibilities": responsibilities,
            },
        ))

    async def log_score(
        self,
        job_id: str,
        overall_score: float,
        reasoning: str,
        matched_keywords: list[str],
        missing_keywords: list[str],
    ) -> None:
        """Log the match scoring result."""
        await self.record(AuditEvent(
            job_id=job_id,
            event_type=AuditEventType.JOB_SCORED,
            content=reasoning,
            metadata={
                "overall_score": overall_score,
                "matched_keywords": matched_keywords,
                "missing_keywords": missing_keywords,
            },
        ))

    async def log_writing_stage(
        self,
        job_id: str,
        application_id: str | None,
        event_type: AuditEventType,
        content: str,
        feedback: str = "",
        changes_made: list[str] | None = None,
    ) -> None:
        """Log a writing pipeline stage (draft, edit, mediation)."""
        await self.record(AuditEvent(
            job_id=job_id,
            application_id=application_id,
            event_type=event_type,
            content=content,
            metadata={
                "feedback": feedback,
                "changes_made": changes_made or [],
            },
        ))

    async def log_application_result(
        self,
        job_id: str,
        application_id: str,
        success: bool,
        session_url: str | None = None,
        error: str | None = None,
    ) -> None:
        """Log the final application submission result."""
        event_type = (
            AuditEventType.APPLICATION_SUBMITTED if success
            else AuditEventType.APPLICATION_FAILED
        )
        await self.record(AuditEvent(
            job_id=job_id,
            application_id=application_id,
            event_type=event_type,
            content=f"{'Success' if success else 'Failed'}: {error or 'Application submitted'}",
            metadata={
                "success": success,
                "session_url": session_url,
                "error": error,
            },
        ))

    async def get_events_for_job(self, job_id: str) -> list[dict]:
        """Retrieve all audit events for a job, ordered chronologically."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM audit_events WHERE job_id = ? ORDER BY created_at",
                (job_id,),
            )
            rows = await cursor.fetchall()
            return [
                {**dict(row), "metadata": json.loads(row["metadata"])}
                for row in rows
            ]

    async def get_events_for_application(self, application_id: str) -> list[dict]:
        """Retrieve all audit events for a specific application."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM audit_events WHERE application_id = ? ORDER BY created_at",
                (application_id,),
            )
            rows = await cursor.fetchall()
            return [
                {**dict(row), "metadata": json.loads(row["metadata"])}
                for row in rows
            ]

    async def _write_event_file(self, event: AuditEvent) -> None:
        """Write event to a structured JSON file for human review."""
        # Organize by job_id/event_type
        job_dir = self.output_dir / event.job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        filename = f"{event.event_type.value}_{event.id}.json"
        filepath = job_dir / filename

        data = {
            "id": event.id,
            "job_id": event.job_id,
            "application_id": event.application_id,
            "event_type": event.event_type.value,
            "created_at": event.created_at.isoformat(),
            "metadata": event.metadata,
            "content": event.content,
        }

        filepath.write_text(json.dumps(data, indent=2, ensure_ascii=False))
