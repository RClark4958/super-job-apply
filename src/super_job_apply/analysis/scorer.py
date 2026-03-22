"""LLM-based job-candidate fit scoring.

Evaluates how well a candidate matches a job posting and returns a structured
MatchScore with recommendation (strong_apply / apply / skip).
"""

from __future__ import annotations

import json
import logging
import os

import anthropic

from ..models import CandidateProfile, JobPosting, MatchRecommendation, MatchScore

logger = logging.getLogger(__name__)

_SCORING_PROMPT = """You are a job matching expert. Evaluate how well this candidate matches the job posting.

## Job Posting
- Title: {job_title}
- Company: {company_name}
- Location: {location}
- Work Type: {work_type}
- Requirements: {requirements}
- Responsibilities: {responsibilities}
- Full Description: {full_description}

## Candidate Profile
- Target Roles: {target_roles}
- Skills: {skills}
- Years of Experience: {years_experience}
- Experience Summary: {experience_summary}
- Education: {education}
- Target Industries: {target_industries}
- Location: {candidate_location}
- Willing to Relocate: {willing_to_relocate}
- Requires Sponsorship: {requires_sponsorship}

## Scoring Instructions
1. Count how many required skills the candidate has vs. total required
2. Evaluate years of experience alignment
3. Consider industry/domain relevance
4. Consider location compatibility
5. Penalize heavy mismatches (e.g., requires 10 years, candidate has 2)

Return a JSON object with EXACTLY these fields:
{{
    "overall_score": <float 0.0-1.0>,
    "skill_match": <float 0.0-1.0>,
    "experience_match": <float 0.0-1.0>,
    "reasoning": "<2-3 sentence explanation>",
    "matched_keywords": ["<skill1>", "<skill2>", ...],
    "missing_keywords": ["<skill1>", "<skill2>", ...]
}}

Return ONLY the JSON object, no other text."""


async def score_job(
    job: JobPosting,
    candidate: CandidateProfile,
    model: str = "claude-sonnet-4-6",
) -> MatchScore:
    """Score how well a candidate matches a job posting using an LLM.

    Args:
        job: The job posting to evaluate.
        candidate: The candidate's profile.
        model: Anthropic model to use for scoring.

    Returns:
        MatchScore with overall score, breakdown, and recommendation.
    """
    try:
        result = await _call_llm(job, candidate, model)
        score = _parse_score(job.id, result)
        logger.info(
            f"Scored {job.company_name} - {job.job_title}: "
            f"{score.overall_score:.2f} ({score.recommendation.value})"
        )
        return score
    except Exception as e:
        logger.warning(f"Scoring failed for {job.company_name} - {job.job_title}: {e}")
        # Return neutral score on failure so job isn't skipped silently
        return MatchScore(
            job_id=job.id,
            overall_score=0.5,
            reasoning=f"Scoring failed: {e}. Defaulting to neutral score for manual review.",
            recommendation=MatchRecommendation.APPLY,
        )


async def _call_llm(job: JobPosting, candidate: CandidateProfile, model: str) -> str:
    """Call the Anthropic API to score the match."""
    client = anthropic.AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    prompt = _SCORING_PROMPT.format(
        job_title=job.job_title,
        company_name=job.company_name,
        location=job.location or "Not specified",
        work_type=job.work_type or "Not specified",
        requirements=", ".join(job.requirements) if job.requirements else "Not specified",
        responsibilities=", ".join(job.responsibilities) if job.responsibilities else "Not specified",
        full_description=job.full_description[:2000] if job.full_description else "Not available",
        target_roles=", ".join(candidate.target_roles),
        skills=", ".join(candidate.skills),
        years_experience=candidate.years_experience,
        experience_summary=candidate.experience_summary,
        education=", ".join(f"{e.degree} from {e.school}" for e in candidate.education),
        target_industries=", ".join(candidate.target_industries),
        candidate_location=candidate.location,
        willing_to_relocate=candidate.willing_to_relocate,
        requires_sponsorship=candidate.requires_sponsorship,
    )

    message = await client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    return message.content[0].text


def _parse_score(job_id: str, raw_response: str) -> MatchScore:
    """Parse the LLM response into a MatchScore."""
    # Strip any markdown code fences
    text = raw_response.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    data = json.loads(text)

    overall = float(data.get("overall_score", 0.0))

    # Map score to recommendation
    if overall >= 0.8:
        recommendation = MatchRecommendation.STRONG_APPLY
    elif overall >= 0.6:
        recommendation = MatchRecommendation.APPLY
    else:
        recommendation = MatchRecommendation.SKIP

    return MatchScore(
        job_id=job_id,
        overall_score=overall,
        skill_match=float(data.get("skill_match", 0.0)),
        experience_match=float(data.get("experience_match", 0.0)),
        reasoning=data.get("reasoning", ""),
        matched_keywords=data.get("matched_keywords", []),
        missing_keywords=data.get("missing_keywords", []),
        recommendation=recommendation,
    )
