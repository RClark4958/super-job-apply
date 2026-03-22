"""Multi-agent writing pipeline: Writer → Editor → Mediator.

Orchestrates three specialized agents to produce human-quality application
materials. Each stage is logged to the audit trail for full transparency.

Flow:
1. Writer creates initial draft (optimized for content + keywords)
2. Editor reviews for accuracy, AI tells, and human tone (returns feedback + edited version)
3. Mediator resolves conflicts between Writer and Editor (produces final version)
"""

from __future__ import annotations

import logging

from ..audit import AuditEventType, AuditTrail
from ..models import CandidateProfile, JobPosting, MatchScore
from .editor import edit_cover_letter, edit_resume
from .mediator import mediate_cover_letter, mediate_resume
from .writer import draft_cover_letter, draft_resume

logger = logging.getLogger(__name__)


async def write_resume(
    job: JobPosting,
    candidate: CandidateProfile,
    score: MatchScore,
    original_content: str,
    audit: AuditTrail,
    application_id: str | None = None,
    model: str = "claude-sonnet-4-6",
) -> str:
    """Run the full Writer → Editor → Mediator pipeline for a resume.

    Args:
        job: Target job posting.
        candidate: Candidate profile.
        score: Match score with keyword analysis.
        original_content: Raw text from the candidate's resume template.
        audit: Audit trail for logging each stage.
        application_id: Optional application ID for audit linkage.
        model: Anthropic model to use for all agents.

    Returns:
        Final mediated resume content as plain text.
    """
    requirements = ", ".join(job.requirements) if job.requirements else "Not specified"
    responsibilities = ", ".join(job.responsibilities) if job.responsibilities else "Not specified"
    description = job.full_description[:2000] if job.full_description else "Not available"
    matched = ", ".join(score.matched_keywords) if score.matched_keywords else "None identified"
    missing = ", ".join(score.missing_keywords) if score.missing_keywords else "None identified"
    skills = ", ".join(candidate.skills)

    # --- Stage 1: Writer ---
    logger.info(f"[{job.company_name}] Resume writer drafting...")
    writer_draft = await draft_resume(
        original_content=original_content,
        job_title=job.job_title,
        company_name=job.company_name,
        requirements=requirements,
        responsibilities=responsibilities,
        description=description,
        matched_keywords=matched,
        missing_keywords=missing,
        experience_summary=candidate.experience_summary,
        skills=skills,
        model=model,
    )

    await audit.log_writing_stage(
        job_id=job.id,
        application_id=application_id,
        event_type=AuditEventType.RESUME_DRAFT,
        content=writer_draft,
    )

    # --- Stage 2: Editor ---
    logger.info(f"[{job.company_name}] Resume editor reviewing...")
    editor_result = await edit_resume(
        draft=writer_draft,
        original_content=original_content,
        candidate_skills=skills,
        experience_summary=candidate.experience_summary,
        job_title=job.job_title,
        company_name=job.company_name,
        model=model,
    )

    editor_content = editor_result.get("edited_content", writer_draft)
    editor_feedback = editor_result.get("feedback", "")
    editor_changes = editor_result.get("changes_made", [])
    accuracy_flags = editor_result.get("accuracy_flags", [])
    ai_tells = editor_result.get("ai_tells_fixed", [])

    await audit.log_writing_stage(
        job_id=job.id,
        application_id=application_id,
        event_type=AuditEventType.RESUME_EDITOR_REVIEW,
        content=editor_content,
        feedback=editor_feedback,
        changes_made=editor_changes + [f"AI tells fixed: {t}" for t in ai_tells],
    )

    # --- Stage 3: Mediator ---
    logger.info(f"[{job.company_name}] Resume mediator finalizing...")
    mediator_result = await mediate_resume(
        writer_draft=writer_draft,
        editor_version=editor_content,
        editor_feedback=editor_feedback,
        editor_changes=editor_changes,
        accuracy_flags=accuracy_flags,
        job_title=job.job_title,
        company_name=job.company_name,
        original_content=original_content,
        model=model,
    )

    final_content = mediator_result.get("final_content", editor_content)
    rationale = mediator_result.get("rationale", "")

    await audit.log_writing_stage(
        job_id=job.id,
        application_id=application_id,
        event_type=AuditEventType.RESUME_MEDIATOR_FINAL,
        content=final_content,
        feedback=rationale,
        changes_made=mediator_result.get("modifications", []),
    )

    logger.info(f"[{job.company_name}] Resume pipeline complete. Mediator: {rationale[:100]}")
    return final_content


async def write_cover_letter(
    job: JobPosting,
    candidate: CandidateProfile,
    score: MatchScore,
    audit: AuditTrail,
    application_id: str | None = None,
    model: str = "claude-sonnet-4-6",
) -> str:
    """Run the full Writer → Editor → Mediator pipeline for a cover letter.

    Args:
        job: Target job posting.
        candidate: Candidate profile.
        score: Match score with keyword analysis.
        audit: Audit trail for logging each stage.
        application_id: Optional application ID for audit linkage.
        model: Anthropic model to use for all agents.

    Returns:
        Final mediated cover letter body text (no greeting/sign-off).
    """
    requirements = ", ".join(job.requirements) if job.requirements else "Not specified"
    responsibilities = ", ".join(job.responsibilities) if job.responsibilities else "Not specified"
    description = job.full_description[:2000] if job.full_description else "Not available"
    matched = ", ".join(score.matched_keywords) if score.matched_keywords else "General alignment"
    missing = ", ".join(score.missing_keywords) if score.missing_keywords else "None identified"
    education = ", ".join(f"{e.degree} from {e.school}" for e in candidate.education)

    # --- Stage 1: Writer ---
    logger.info(f"[{job.company_name}] Cover letter writer drafting...")
    writer_draft = await draft_cover_letter(
        job_title=job.job_title,
        company_name=job.company_name,
        location=job.location or "Not specified",
        requirements=requirements,
        responsibilities=responsibilities,
        description=description,
        candidate_name=candidate.name,
        candidate_location=candidate.location,
        skills=", ".join(candidate.skills),
        years_experience=candidate.years_experience,
        experience_summary=candidate.experience_summary,
        education=education,
        matched_keywords=matched,
        missing_keywords=missing,
        reasoning=score.reasoning or "No detailed analysis available",
        model=model,
    )

    await audit.log_writing_stage(
        job_id=job.id,
        application_id=application_id,
        event_type=AuditEventType.COVER_LETTER_DRAFT,
        content=writer_draft,
    )

    # --- Stage 2: Editor ---
    logger.info(f"[{job.company_name}] Cover letter editor reviewing...")
    editor_result = await edit_cover_letter(
        draft=writer_draft,
        candidate_name=candidate.name,
        experience_summary=candidate.experience_summary,
        job_title=job.job_title,
        company_name=job.company_name,
        model=model,
    )

    editor_content = editor_result.get("edited_content", writer_draft)
    editor_feedback = editor_result.get("feedback", "")
    editor_changes = editor_result.get("changes_made", [])
    ai_tells = editor_result.get("ai_tells_fixed", [])

    await audit.log_writing_stage(
        job_id=job.id,
        application_id=application_id,
        event_type=AuditEventType.COVER_LETTER_EDITOR_REVIEW,
        content=editor_content,
        feedback=editor_feedback,
        changes_made=editor_changes + [f"AI tells fixed: {t}" for t in ai_tells],
    )

    # --- Stage 3: Mediator ---
    logger.info(f"[{job.company_name}] Cover letter mediator finalizing...")
    mediator_result = await mediate_cover_letter(
        writer_draft=writer_draft,
        editor_version=editor_content,
        editor_feedback=editor_feedback,
        editor_changes=editor_changes,
        candidate_name=candidate.name,
        job_title=job.job_title,
        company_name=job.company_name,
        model=model,
    )

    final_content = mediator_result.get("final_content", editor_content)
    rationale = mediator_result.get("rationale", "")

    await audit.log_writing_stage(
        job_id=job.id,
        application_id=application_id,
        event_type=AuditEventType.COVER_LETTER_MEDIATOR_FINAL,
        content=final_content,
        feedback=rationale,
        changes_made=mediator_result.get("modifications", []),
    )

    logger.info(f"[{job.company_name}] Cover letter pipeline complete. Mediator: {rationale[:100]}")
    return final_content
