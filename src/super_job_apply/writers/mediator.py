"""Mediator agent — resolves conflicts between Writer and Editor.

When the Editor makes changes the Writer might disagree with (e.g., removing
a keyword the Writer intentionally placed, or softening language that was
strategically strong), the Mediator reviews both versions and the Editor's
feedback, then produces the final version with a clear rationale.
"""

from __future__ import annotations

import json
import os

import anthropic


async def mediate_resume(
    writer_draft: str,
    editor_version: str,
    editor_feedback: str,
    editor_changes: list[str],
    accuracy_flags: list[str],
    job_title: str,
    company_name: str,
    original_content: str,
    model: str = "claude-sonnet-4-6",
) -> dict:
    """Mediate between Writer and Editor versions of a resume.

    Returns:
        Dict with keys: final_content, rationale, kept_from_writer,
        kept_from_editor, modifications
    """
    client = anthropic.AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    prompt = f"""You are a senior content strategist mediating between a resume
Writer and an Editor. Both have valid perspectives — the Writer optimizes for
ATS keywords and impact, the Editor optimizes for authenticity and human tone.
Your job is to produce the best possible final version.

## Context
Role: {job_title} at {company_name}

## Writer's Draft
{writer_draft}

## Editor's Version
{editor_version}

## Editor's Feedback
{editor_feedback}

## Specific Changes the Editor Made
{json.dumps(editor_changes, indent=2)}

## Accuracy Concerns Raised
{json.dumps(accuracy_flags, indent=2)}

## Candidate's Original Resume (ground truth)
{original_content}

## Your Mediation Rules

1. **Accuracy flags always win.** If the Editor flagged something as potentially
   fabricated and the original resume doesn't support it, remove it or soften it
   regardless of keyword value.

2. **Keywords matter, but not at the cost of sounding robotic.** If the Writer
   placed a keyword and the Editor removed it for sounding forced, find a middle
   ground — reword the bullet to include the keyword naturally.

3. **Variety in sentence structure is non-negotiable.** If the Writer's version
   has 8 bullets all starting with action verbs in the same pattern, break it up
   even if the Editor's version went too far the other way.

4. **Prefer the Editor's version when the changes are about tone.** The Editor
   is calibrated for how humans actually write. Trust their instinct on phrasing.

5. **Prefer the Writer's version when the changes are about content ordering
   or emphasis.** The Writer understands what this specific job posting values.

6. **Don't add anything new.** Your job is to synthesize, not create.

Return a JSON object:
{{
    "final_content": "<the mediated resume in ## Section / - bullet format>",
    "rationale": "<3-5 sentences explaining your key decisions>",
    "kept_from_writer": ["<elements preserved from the Writer's draft>"],
    "kept_from_editor": ["<elements preserved from the Editor's version>"],
    "modifications": ["<any additional tweaks you made and why>"]
}}

Return ONLY the JSON object."""

    message = await client.messages.create(
        model=model,
        max_tokens=6000,
        messages=[{"role": "user", "content": prompt}],
    )
    return _parse_json_response(message.content[0].text)


async def mediate_cover_letter(
    writer_draft: str,
    editor_version: str,
    editor_feedback: str,
    editor_changes: list[str],
    candidate_name: str,
    job_title: str,
    company_name: str,
    model: str = "claude-sonnet-4-6",
) -> dict:
    """Mediate between Writer and Editor versions of a cover letter.

    Returns:
        Dict with keys: final_content, rationale, kept_from_writer,
        kept_from_editor, modifications
    """
    client = anthropic.AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    prompt = f"""You are mediating between a cover letter Writer and Editor for
{candidate_name}'s application to {job_title} at {company_name}.

## Writer's Draft
{writer_draft}

## Editor's Version
{editor_version}

## Editor's Feedback
{editor_feedback}

## Editor's Changes
{json.dumps(editor_changes, indent=2)}

## Your Mediation Priorities (in order)

1. **Does it sound like a real person wrote it?** Read it aloud. If any sentence
   makes you cringe or sounds like a template, fix it. This is the #1 priority.

2. **Is every claim backed by the candidate's actual experience?** Remove
   anything that sounds made up, even if it would be impressive.

3. **Is there at least one specific detail about the company?** Not "your
   innovative approach" — an actual product, initiative, or fact. If neither
   version has this, note it in your rationale (but don't invent one).

4. **Is it under 350 words?** Brevity signals confidence. If the combined best
   parts are too long, cut the weakest paragraph rather than trimming everything.

5. **Does the opening hook?** The first sentence should make someone want to
   read the second. If neither version nails this, pick the better one and
   sharpen it.

Return a JSON object:
{{
    "final_content": "<the mediated cover letter body — no greeting or sign-off>",
    "rationale": "<3-5 sentences explaining your key decisions>",
    "kept_from_writer": ["<elements from the Writer's version>"],
    "kept_from_editor": ["<elements from the Editor's version>"],
    "modifications": ["<your own tweaks and why>"]
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
