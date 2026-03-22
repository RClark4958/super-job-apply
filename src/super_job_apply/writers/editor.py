"""Editor agent — reviews drafts for accuracy, tone, and human quality.

The Editor acts as a skeptical, experienced hiring manager who has read
thousands of applications. It catches AI-sounding language, fabricated
claims, keyword stuffing, and generic filler — then provides specific
line-level feedback and a corrected version.
"""

from __future__ import annotations

import json
import os

import anthropic


async def edit_resume(
    draft: str,
    original_content: str,
    candidate_skills: str,
    experience_summary: str,
    job_title: str,
    company_name: str,
    model: str = "claude-sonnet-4-6",
) -> dict:
    """Review and edit a resume draft for accuracy and human quality.

    Returns:
        Dict with keys: edited_content, feedback, changes_made
    """
    client = anthropic.AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    prompt = f"""You are a senior hiring manager and resume editor. You've read
10,000+ resumes and can instantly spot AI-generated content. Your job is to
review this resume draft and make it sound like it was written by a thoughtful
human, not a language model.

## Draft to Review
{draft}

## Candidate's Original Resume (for accuracy checking)
{original_content}

## Candidate's Actual Background
Skills: {candidate_skills}
Experience: {experience_summary}

## Target Role
{job_title} at {company_name}

## Your Review Checklist

**Accuracy** (most important)
- Flag anything that appears fabricated or inflated beyond what the original
  resume and experience summary support
- Check that numbers and metrics aren't invented
- Ensure job titles and responsibilities match the original

**AI Detection** — flag and fix these common tells:
- Every bullet starting with a power verb ("Spearheaded," "Orchestrated,"
  "Championed") — real resumes have more variety
- Buzzword density: "cutting-edge," "innovative solutions," "drove impactful
  results" — replace with plain, specific language
- Perfectly parallel structure in every bullet — humans are slightly messier
- Overuse of quantifiers: "Increased X by Y%" on every single bullet feels fake
  when the original didn't have metrics
- Exclamation-point energy in a resume context

**Keyword Integration**
- Keywords should flow naturally, not be jammed in
- Check that keyword placement makes contextual sense

**Readability**
- Bullets that are too long (over 2 lines) should be tightened
- Remove filler words: "effectively," "successfully," "in order to"
- Ensure consistent tense (past for previous roles, present for current)

Return a JSON object with exactly these fields:
{{
    "edited_content": "<the full corrected resume in the same format as the draft>",
    "feedback": "<2-4 sentences summarizing what you changed and why>",
    "changes_made": ["<specific change 1>", "<specific change 2>", ...],
    "accuracy_flags": ["<any concerns about truthfulness>"],
    "ai_tells_fixed": ["<specific AI-sounding phrases you replaced>"]
}}

Return ONLY the JSON object."""

    message = await client.messages.create(
        model=model,
        max_tokens=6000,
        messages=[{"role": "user", "content": prompt}],
    )
    return _parse_json_response(message.content[0].text)


async def edit_cover_letter(
    draft: str,
    candidate_name: str,
    experience_summary: str,
    job_title: str,
    company_name: str,
    model: str = "claude-sonnet-4-6",
) -> dict:
    """Review and edit a cover letter draft for tone and authenticity.

    Returns:
        Dict with keys: edited_content, feedback, changes_made
    """
    client = anthropic.AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    prompt = f"""You are an editor who specializes in making professional writing
sound human. You're reviewing a cover letter draft for {candidate_name} applying
to {job_title} at {company_name}.

## Draft to Review
{draft}

## Candidate's Actual Background
{experience_summary}

## Your Review — The "Read It Aloud" Test

Read every sentence aloud in your head. If it sounds like something a person
would never actually say in conversation, it needs rewriting.

**Kill these on sight:**
- "I am thrilled / excited / passionate about this opportunity" — everyone
  writes this. Replace with something specific about the company's work.
- "I believe I would be a great fit" — show, don't tell. The evidence
  should make this obvious without stating it.
- "Extensive experience in..." — this is a nothing phrase. Replace with
  a specific accomplishment.
- "As a [adjective] professional..." — don't self-label. Demonstrate it.
- "I am confident that..." — confidence comes through in specifics, not
  self-assessment.
- "Utilize" (just say "use"), "endeavor" (just say "try" or "work"),
  "leverage" (just say "use" or "build on")
- Any sentence over 30 words — break it up or tighten it.

**Add these if missing:**
- At least one specific, concrete detail about the company (not just
  their industry — something they actually did or built)
- At least one accomplishment with a number or outcome
- A natural transition between paragraphs (not "Furthermore" or
  "Additionally" — those are essay transitions, not conversation)

**Tone calibration:**
- Professional but warm. Think "smart colleague at a coffee shop,"
  not "formal letter to a judge."
- Confident but not arrogant. "I built X that did Y" not
  "I am uniquely positioned to revolutionize..."
- Brief. If you can cut a sentence without losing meaning, cut it.

Return a JSON object with exactly these fields:
{{
    "edited_content": "<the corrected cover letter body — no greeting or sign-off>",
    "feedback": "<2-4 sentences on what you changed and why>",
    "changes_made": ["<specific change 1>", "<specific change 2>", ...],
    "ai_tells_fixed": ["<AI-sounding phrases you replaced and what you used instead>"]
}}

Return ONLY the JSON object."""

    message = await client.messages.create(
        model=model,
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    return _parse_json_response(message.content[0].text)


def _parse_json_response(raw: str) -> dict:
    """Parse JSON from LLM response, handling code fences."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
    return json.loads(text)
