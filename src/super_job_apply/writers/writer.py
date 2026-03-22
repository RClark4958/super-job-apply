"""Writer agent — creates initial drafts of resumes and cover letters.

The Writer focuses on strong content: ATS keyword integration, relevant
achievement highlighting, and compelling narrative. It prioritizes
substance and job-specific alignment over polish.
"""

from __future__ import annotations

import os

import anthropic


async def draft_resume(
    original_content: str,
    job_title: str,
    company_name: str,
    requirements: str,
    responsibilities: str,
    description: str,
    matched_keywords: str,
    missing_keywords: str,
    experience_summary: str,
    skills: str,
    model: str = "claude-sonnet-4-6",
) -> str:
    """Create an initial draft of a tailored resume."""
    client = anthropic.AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    prompt = f"""You are a professional resume writer. Your job is to rewrite this
resume so it speaks directly to the job posting below. Think of yourself as a
career coach sitting across from the candidate — you know their real experience,
and you're helping them present it in the language this employer uses.

## The Job
**{job_title}** at **{company_name}**

Requirements: {requirements}
Responsibilities: {responsibilities}
Description (excerpt): {description[:1500]}

## Keywords Analysis
Already present in resume: {matched_keywords}
Missing — weave in where honest: {missing_keywords}

## The Candidate's Current Resume
{original_content}

## Their Background
{experience_summary}
Skills: {skills}

## Your Approach
- Mirror the job posting's phrasing. If they say "cross-functional collaboration,"
  don't write "worked with other teams."
- Lead each bullet with a strong verb and a number when possible.
  "Reduced API latency 40% by..." not "Was responsible for improving..."
- Reorder bullets so the most relevant hits appear first in each section.
- Keep section headers from the original — don't invent new ones.
- Never fabricate. If the candidate hasn't done it, don't claim it.
  Instead, find adjacent experience that maps to the requirement.
- Vary your sentence openings. Real humans don't start every bullet
  with "Spearheaded" or "Leveraged." Mix in natural phrasing like
  "Built the...", "Cut costs by...", "Owned the..."
- Keep each bullet to one or two lines. Hiring managers skim.

Return the resume as plain text:
- "# Name" for the candidate name header
- "## Section Name" for section headers
- "- " for bullet points
- Plain text for summary paragraphs

Return ONLY the resume content."""

    message = await client.messages.create(
        model=model,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


async def draft_cover_letter(
    job_title: str,
    company_name: str,
    location: str,
    requirements: str,
    responsibilities: str,
    description: str,
    candidate_name: str,
    candidate_location: str,
    skills: str,
    years_experience: int,
    experience_summary: str,
    education: str,
    matched_keywords: str,
    missing_keywords: str,
    reasoning: str,
    model: str = "claude-sonnet-4-6",
) -> str:
    """Create an initial draft of a tailored cover letter."""
    client = anthropic.AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    prompt = f"""Write a cover letter for {candidate_name} applying to **{job_title}**
at **{company_name}**.

## About the Role
Location: {location}
Requirements: {requirements}
Responsibilities: {responsibilities}
Description: {description[:1500]}

## About the Candidate
Location: {candidate_location} | {years_experience} years experience
Skills: {skills}
Background: {experience_summary}
Education: {education}
Key strengths for this role: {matched_keywords}
Gaps to address: {missing_keywords}
Why they could fit: {reasoning}

## Writing Guidelines
Write like a real person, not a template. Here's what that means:

**Opening** — Don't start with "I am writing to express my interest." Instead,
open with something specific: a product the company shipped, a problem they're
solving, a trend they're leading. Show you actually looked them up. One sentence
that proves you're not mass-applying.

**Body (2 paragraphs)** — Pick 2-3 accomplishments that directly map to their
requirements. Be specific: numbers, outcomes, team size, timeline. Don't just
list skills — tell tiny stories. "When we needed to cut onboarding time, I built
a..." reads human; "I have extensive experience in..." reads like a bot.

For any skill gaps, don't apologize. Reframe: "My work on X gave me a strong
foundation for Y" or "I've been diving into Z through..." Confidence without
dishonesty.

**Closing** — Brief. Express genuine interest in a specific aspect of their work
(not generic "this exciting opportunity"). Don't beg or grovel. End with a
forward-looking statement about contributing to something specific they care about.

**Tone** — Professional but conversational. Read it aloud — if it sounds like
something you'd never actually say to a person, rewrite it. Avoid corporate
buzzwords: "synergy," "leverage," "passionate about." Use plain language.

**Length** — 250-350 words. Three to four paragraphs max. Respect their time.

Return ONLY the cover letter body paragraphs. No "Dear Hiring Manager" or
sign-off — those are added separately."""

    message = await client.messages.create(
        model=model,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text
