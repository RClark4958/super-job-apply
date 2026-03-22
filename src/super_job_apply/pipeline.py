"""Main pipeline orchestrator.

Wires together discovery → dedup → score → filter → tailor → apply → report.
Every significant event is recorded to the audit trail for review.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

from .analysis.scorer import score_job
from .applicator.browser import apply_with_retry
from .audit import AuditTrail
from .db import Database
from .discovery.exa_company import ExaCompanySource
from .discovery.exa_jobs import ExaJobSource
from .discovery.greenhouse_lever import GreenhouseLeverSource
from .discovery.linkedin_brightdata import LinkedInBrightDataSource
from .models import (
    AppConfig,
    Application,
    ApplicationStatus,
    MatchRecommendation,
)
from .tailoring.cover_letter import generate_cover_letter
from .tailoring.resume import tailor_resume

logger = logging.getLogger(__name__)
console = Console()


async def run_pipeline(config: AppConfig) -> None:
    """Execute the full job application pipeline.

    Steps:
        1. Initialize database and audit trail
        2. Discover jobs from all sources
        3. Deduplicate against existing DB records
        4. Score each job for candidate fit
        5. Filter out poor matches
        6. Tailor resume and cover letter (Writer → Editor → Mediator pipeline)
        7. Apply to each job (unless dry_run)
        8. Print summary report
    """
    settings = config.application
    candidate = config.candidate
    db = Database(settings.db_path)
    await db.init()

    audit = AuditTrail(db_path=settings.db_path, output_dir=settings.output_dir)
    await audit.init()

    # --- Step 1: Discover ---
    console.print("\n[bold blue]Step 1: Discovering jobs...[/bold blue]")
    sources = [
        GreenhouseLeverSource(),   # Direct ATS boards — best quality, no aggregator noise
        # LinkedInBrightDataSource(),  # Disabled: Bright Data polling takes 5+ min per query
        ExaCompanySource(),         # Exa company search — finds careers pages
        ExaJobSource(),             # Exa direct job search — broad coverage
    ]
    all_jobs = []

    for source in sources:
        try:
            jobs = await source.discover(config.search)
            all_jobs.extend(jobs)
            console.print(f"  {source.source_name}: found {len(jobs)} jobs")
        except Exception as e:
            logger.warning(f"Source {source.source_name} failed: {e}")
            console.print(f"  [yellow]{source.source_name}: failed ({e})[/yellow]")

    if not all_jobs:
        console.print("[red]No jobs found. Check your search criteria.[/red]")
        return

    # --- Step 2: Deduplicate ---
    console.print("\n[bold blue]Step 2: Deduplicating...[/bold blue]")
    new_jobs = []
    for job in all_jobs:
        if not await db.job_exists(job.company_name, job.job_title):
            job_id = await db.insert_job(job)
            job.id = job_id
            new_jobs.append(job)

            # Audit: log the full job listing content
            await audit.log_job_listing(
                job_id=job.id,
                company=job.company_name,
                title=job.job_title,
                full_description=job.full_description,
                careers_url=job.careers_url,
                requirements=job.requirements,
                responsibilities=job.responsibilities,
            )

    console.print(
        f"  {len(all_jobs)} discovered, {len(all_jobs) - len(new_jobs)} duplicates removed, "
        f"{len(new_jobs)} new jobs"
    )

    if not new_jobs:
        console.print("[yellow]No new jobs to process.[/yellow]")
        return

    # --- Step 2b: Resolve aggregator URLs ---
    from .discovery.url_resolver import is_aggregator, resolve_to_direct_url

    aggregator_jobs = [j for j in new_jobs if is_aggregator(j.careers_url)]
    if aggregator_jobs:
        console.print(f"\n[bold blue]Step 2b: Resolving {len(aggregator_jobs)} aggregator URLs...[/bold blue]")
        resolved_count = 0
        for job in aggregator_jobs:
            direct_url = await resolve_to_direct_url(
                job.company_name, job.job_title, job.careers_url
            )
            if direct_url and not is_aggregator(direct_url):
                old_url = job.careers_url
                job.careers_url = direct_url
                # Update in DB too
                async with __import__("aiosqlite").connect(settings.db_path) as _db:
                    await _db.execute(
                        "UPDATE jobs SET careers_url = ? WHERE id = ?",
                        (direct_url, job.id)
                    )
                    await _db.commit()
                resolved_count += 1
                console.print(f"  [green]Resolved:[/green] {job.company_name} → {direct_url[:60]}")
            else:
                # Remove from new_jobs — can't apply to unresolved aggregator
                new_jobs.remove(job)
                console.print(f"  [dim]Skipped (unresolvable): {job.company_name}[/dim]")
            await asyncio.sleep(2)  # Rate limit Exa
        console.print(f"  {resolved_count} resolved, {len(aggregator_jobs) - resolved_count} removed")

    # --- Step 3: Score ---
    console.print("\n[bold blue]Step 3: Scoring job-candidate fit...[/bold blue]")
    scored_jobs = []
    for job in new_jobs:
        score = await score_job(job, candidate, settings.tailoring_model)
        scored_jobs.append((job, score))

        # Audit: log scoring result
        await audit.log_score(
            job_id=job.id,
            overall_score=score.overall_score,
            reasoning=score.reasoning,
            matched_keywords=score.matched_keywords,
            missing_keywords=score.missing_keywords,
        )

        status = {
            MatchRecommendation.STRONG_APPLY: "[bold green]STRONG[/bold green]",
            MatchRecommendation.APPLY: "[green]APPLY[/green]",
            MatchRecommendation.SKIP: "[dim]SKIP[/dim]",
        }.get(score.recommendation, "???")
        console.print(
            f"  {score.overall_score:.2f} {status} {job.company_name} - {job.job_title}"
        )

    # --- Step 4: Filter ---
    console.print(f"\n[bold blue]Step 4: Filtering (min score: {settings.min_match_score})...[/bold blue]")
    qualifying = [(j, s) for j, s in scored_jobs if s.overall_score >= settings.min_match_score]
    skipped = [(j, s) for j, s in scored_jobs if s.overall_score < settings.min_match_score]

    for job, score in skipped:
        app = Application(
            job_id=job.id,
            status=ApplicationStatus.SKIPPED,
            match_score=score.overall_score,
        )
        await db.insert_application(app)

    console.print(f"  {len(qualifying)} qualifying, {len(skipped)} skipped")

    if not qualifying:
        console.print("[yellow]No jobs met the minimum match score.[/yellow]")
        return

    # --- Step 5: Tailor (Writer → Editor → Mediator pipeline) ---
    TAILOR_CONCURRENCY = 10
    console.print(
        f"\n[bold blue]Step 5: Tailoring application materials "
        f"({TAILOR_CONCURRENCY} concurrent sessions)...[/bold blue]"
    )
    tailored_jobs = []
    tailor_semaphore = asyncio.Semaphore(TAILOR_CONCURRENCY)
    tailor_count = 0

    async def _tailor_one(job, score, index, total):
        nonlocal tailor_count
        async with tailor_semaphore:
            resume_path = None
            cover_letter_path = None
            cover_letter_text = None

            try:
                if Path(settings.resume_template_path).exists():
                    resume_path = await tailor_resume(
                        job, candidate, score,
                        settings.resume_template_path,
                        settings.output_dir,
                        audit=audit,
                        model=settings.tailoring_model,
                    )
            except Exception as e:
                logger.warning(f"Resume tailoring failed for {job.company_name}: {e}")

            try:
                cover_letter_path, cover_letter_text = await generate_cover_letter(
                    job, candidate, score,
                    settings.output_dir,
                    audit=audit,
                    model=settings.tailoring_model,
                )
            except Exception as e:
                logger.warning(f"Cover letter generation failed for {job.company_name}: {e}")

            tailor_count += 1
            console.print(f"  [{tailor_count}/{total}] {job.company_name} — {job.job_title}")
            return (job, score, resume_path, cover_letter_path, cover_letter_text)

    tailor_tasks = [
        _tailor_one(job, score, i, len(qualifying))
        for i, (job, score) in enumerate(qualifying, 1)
    ]
    tailored_jobs = await asyncio.gather(*tailor_tasks)

    # --- Step 6: Apply ---
    if settings.dry_run:
        console.print("\n[bold yellow]DRY RUN — skipping actual applications.[/bold yellow]")
        for job, score, resume_path, cl_path, _ in tailored_jobs:
            app = Application(
                job_id=job.id,
                status=ApplicationStatus.PENDING,
                match_score=score.overall_score,
                resume_path=resume_path,
                cover_letter_path=cl_path,
            )
            await db.insert_application(app)
        console.print(f"  {len(tailored_jobs)} applications prepared (not submitted)")
        console.print(f"  Review materials in: {settings.output_dir}/")
        console.print(f"  Review audit trail in: {settings.output_dir}/audit/")
    else:
        console.print(
            f"\n[bold blue]Step 6: Applying to {len(tailored_jobs)} jobs"
            f" ({'concurrent' if settings.concurrent else 'sequential'})...[/bold blue]"
        )

        if settings.concurrent:
            results = await _apply_concurrent(tailored_jobs, candidate, settings, db, audit)
        else:
            results = await _apply_sequential(tailored_jobs, candidate, settings, db, audit)

        _print_summary(results)

    # --- Final stats ---
    stats = await db.get_stats()
    console.print("\n[bold]Database Stats:[/bold]")
    console.print(f"  Jobs discovered: {stats['jobs_discovered']}")
    console.print(f"  Total applications: {stats['total_applications']}")
    console.print(f"  Avg match score: {stats['avg_match_score']:.2f}")
    for status, count in stats.get("by_status", {}).items():
        console.print(f"    {status}: {count}")


async def _apply_concurrent(
    tailored_jobs: list,
    candidate,
    settings,
    db: Database,
    audit: AuditTrail,
) -> list[dict]:
    """Apply to jobs concurrently with limited parallelism."""
    results = []
    for i in range(0, len(tailored_jobs), settings.max_concurrent_browsers):
        chunk = tailored_jobs[i : i + settings.max_concurrent_browsers]
        chunk_results = await asyncio.gather(
            *[
                _apply_single(job, score, resume_path, cl_text, candidate, settings, db, audit)
                for job, score, resume_path, _, cl_text in chunk
            ]
        )
        results.extend(chunk_results)
    return results


async def _apply_sequential(
    tailored_jobs: list,
    candidate,
    settings,
    db: Database,
    audit: AuditTrail,
) -> list[dict]:
    """Apply to jobs one at a time."""
    results = []
    for job, score, resume_path, _, cl_text in tailored_jobs:
        result = await _apply_single(job, score, resume_path, cl_text, candidate, settings, db, audit)
        results.append(result)
    return results


async def _apply_single(
    job, score, resume_path, cover_letter_text, candidate, settings, db: Database, audit: AuditTrail
) -> dict:
    """Apply to a single job, record the result, and log to audit."""
    app = Application(
        job_id=job.id,
        status=ApplicationStatus.PENDING,
        match_score=score.overall_score,
        resume_path=resume_path,
    )
    await db.insert_application(app)

    result = await apply_with_retry(
        job, candidate, settings, resume_path, cover_letter_text
    )

    success = result.get("success", False)
    new_status = ApplicationStatus.APPLIED if success else ApplicationStatus.FAILED
    await db.update_application(
        app.id,
        status=new_status,
        session_url=result.get("session_url"),
        error_message=result.get("message") if not success else None,
        applied_at=datetime.now(timezone.utc) if success else None,
        retry_count=result.get("retry_count", 0),
    )

    # Audit: log application result
    await audit.log_application_result(
        job_id=job.id,
        application_id=app.id,
        success=success,
        session_url=result.get("session_url"),
        error=result.get("message") if not success else None,
    )

    result["company"] = job.company_name
    result["job_title"] = job.job_title
    result["match_score"] = score.overall_score
    return result


def _print_summary(results: list[dict]) -> None:
    """Print application results summary."""
    console.print("\n" + "=" * 60)
    console.print("[bold]APPLICATION SUMMARY[/bold]")
    console.print("=" * 60)

    successful = [r for r in results if r.get("success")]
    failed = [r for r in results if not r.get("success")]

    console.print(
        f"\nTotal: {len(results)} | "
        f"[green]Success: {len(successful)}[/green] | "
        f"[red]Failed: {len(failed)}[/red]\n"
    )

    for i, r in enumerate(results, 1):
        status = "[green]SUCCESS[/green]" if r.get("success") else "[red]FAILED[/red]"
        console.print(
            f"  {i}. {status} {r['company']} - {r['job_title']} "
            f"(score: {r.get('match_score', 0):.2f})"
        )
        if r.get("session_url"):
            console.print(f"     Session: {r['session_url']}")
        if not r.get("success") and r.get("message"):
            console.print(f"     Error: {r['message'][:100]}")
