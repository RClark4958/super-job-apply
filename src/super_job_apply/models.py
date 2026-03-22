"""Data models for the job application pipeline."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


# --- Candidate / Config Models ---


class Education(BaseModel):
    degree: str
    school: str
    year: int


class CandidateProfile(BaseModel):
    name: str
    email: str
    account_email: str = ""  # Email for account creation (if different from primary)
    phone: str
    linkedin_url: str
    portfolio_url: str | None = None
    location: str
    willing_to_relocate: bool = False
    requires_sponsorship: bool = False
    visa_status: str = ""
    skills: list[str] = Field(default_factory=list)
    years_experience: int = 0
    experience_summary: str = ""
    education: list[Education] = Field(default_factory=list)
    target_roles: list[str] = Field(default_factory=list)
    target_industries: list[str] = Field(default_factory=list)


class SearchCriteria(BaseModel):
    queries: list[str]
    locations: list[str] = Field(default_factory=list)
    num_results_per_query: int = 10
    exclude_domains: list[str] = Field(default_factory=lambda: ["linkedin.com"])
    date_range_days: int | None = 30


class ApplicationSettings(BaseModel):
    min_match_score: float = 0.6
    concurrent: bool = True
    max_concurrent_browsers: int = 3
    use_proxy: bool = True
    dry_run: bool = False
    max_retries: int = 2
    resume_template_path: str = "./resume_template.docx"
    output_dir: str = "./output"
    model: str = "google/gemini-2.5-pro"
    agent_model: str = "google/gemini-2.5-flash"
    tailoring_model: str = "claude-sonnet-4-6"
    db_path: str = "./applications.db"
    account_password: str = ""


class AppConfig(BaseModel):
    candidate: CandidateProfile
    search: SearchCriteria
    application: ApplicationSettings = Field(default_factory=ApplicationSettings)


# --- Pipeline Data Models ---


class JobPosting(BaseModel):
    id: str = Field(default_factory=_new_id)
    source: str = ""  # "exa_company" | "exa_jobs"
    company_name: str
    job_title: str
    careers_url: str
    company_url: str | None = None
    location: str | None = None
    work_type: str | None = None  # remote / hybrid / onsite
    requirements: list[str] = Field(default_factory=list)
    responsibilities: list[str] = Field(default_factory=list)
    benefits: list[str] = Field(default_factory=list)
    full_description: str = ""
    discovered_at: datetime = Field(default_factory=_utcnow)


class MatchRecommendation(str, Enum):
    STRONG_APPLY = "strong_apply"
    APPLY = "apply"
    SKIP = "skip"


class MatchScore(BaseModel):
    job_id: str
    overall_score: float = 0.0
    skill_match: float = 0.0
    experience_match: float = 0.0
    reasoning: str = ""
    matched_keywords: list[str] = Field(default_factory=list)
    missing_keywords: list[str] = Field(default_factory=list)
    recommendation: MatchRecommendation = MatchRecommendation.SKIP


class ApplicationStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    APPLIED = "applied"
    FAILED = "failed"
    SKIPPED = "skipped"
    INTERVIEW = "interview"
    REJECTED = "rejected"
    OFFER = "offer"


class Application(BaseModel):
    id: str = Field(default_factory=_new_id)
    job_id: str
    status: ApplicationStatus = ApplicationStatus.PENDING
    match_score: float | None = None
    resume_path: str | None = None
    cover_letter_path: str | None = None
    session_url: str | None = None
    error_message: str | None = None
    applied_at: datetime | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    retry_count: int = 0


# --- Stagehand extraction schema (used by discovery + applicator) ---

JOB_DESCRIPTION_SCHEMA = {
    "type": "object",
    "properties": {
        "jobTitle": {"type": "string", "description": "The job title"},
        "companyName": {"type": "string", "description": "The company name"},
        "requirements": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Job requirements",
        },
        "responsibilities": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Job responsibilities",
        },
        "benefits": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Job benefits",
        },
        "location": {"type": "string", "description": "Job location"},
        "workType": {"type": "string", "description": "Remote, hybrid, or on-site"},
        "fullDescription": {"type": "string", "description": "Full job description text"},
    },
}
