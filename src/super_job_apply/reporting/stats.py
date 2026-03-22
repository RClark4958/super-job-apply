"""Application statistics and recommendations."""

from __future__ import annotations

import asyncio
import csv
import io
import json

from rich.console import Console
from rich.table import Table

from ..db import Database
from ..models import ApplicationStatus

console = Console()


async def show_stats(db: Database) -> None:
    """Display aggregated application statistics."""
    stats = await db.get_stats()

    console.print("\n[bold]Application Statistics[/bold]")
    console.print("=" * 40)
    console.print(f"  Jobs discovered:     {stats['jobs_discovered']}")
    console.print(f"  Total applications:  {stats['total_applications']}")
    console.print(f"  Avg match score:     {stats['avg_match_score']:.2f}")
    console.print()

    if stats["by_status"]:
        table = Table(title="Applications by Status")
        table.add_column("Status", style="bold")
        table.add_column("Count", justify="right")

        for status, count in sorted(stats["by_status"].items()):
            style = {
                "applied": "green",
                "interview": "bold green",
                "offer": "bold cyan",
                "failed": "red",
                "skipped": "dim",
                "rejected": "red",
                "pending": "yellow",
            }.get(status, "")
            table.add_row(status, str(count), style=style)

        console.print(table)

    # Recommendations
    total = stats["total_applications"]
    applied = stats["by_status"].get("applied", 0)
    interviews = stats["by_status"].get("interview", 0)

    if total > 0:
        console.print("\n[bold]Recommendations[/bold]")
        if applied >= 50 and interviews == 0:
            console.print(
                "  [yellow]Warning: 50+ applications with no interviews. "
                "Consider narrowing your search or improving your resume keywords.[/yellow]"
            )
        elif applied > 0:
            response_rate = (interviews / applied) * 100
            console.print(f"  Response rate: {response_rate:.1f}%")
            if response_rate < 3 and applied >= 20:
                console.print(
                    "  [yellow]Your response rate is below 3%. Consider:[/yellow]\n"
                    "    - Raising min_match_score to focus on better-fit roles\n"
                    "    - Updating your skills or experience summary\n"
                    "    - Reviewing tailored resumes for keyword alignment"
                )
            elif response_rate >= 10:
                console.print("  [green]Strong response rate! Keep your current strategy.[/green]")


async def show_recent(db: Database, count: int = 10) -> None:
    """Display the most recent applications."""
    apps = await db.get_applications(limit=count)

    if not apps:
        console.print("[yellow]No applications found.[/yellow]")
        return

    table = Table(title=f"Recent Applications (last {count})")
    table.add_column("ID", style="dim")
    table.add_column("Company")
    table.add_column("Job Title")
    table.add_column("Score", justify="right")
    table.add_column("Status")
    table.add_column("Date")

    for i, app in enumerate(apps, 1):
        status_style = {
            "applied": "green",
            "interview": "bold green",
            "offer": "bold cyan",
            "failed": "red",
            "skipped": "dim",
            "rejected": "red",
            "pending": "yellow",
        }.get(app["status"], "")

        score = f"{app['match_score']:.2f}" if app.get("match_score") else "—"
        date = app.get("applied_at") or app.get("created_at", "—")
        if isinstance(date, str) and "T" in date:
            date = date.split("T")[0]

        table.add_row(
            app.get("id", "—")[:12],
            app.get("company_name", "—"),
            app.get("job_title", "—"),
            score,
            app["status"],
            date,
            style=status_style,
        )

    console.print(table)


async def export_data(db: Database, fmt: str = "csv") -> str:
    """Export application data as CSV or JSON string."""
    apps = await db.get_applications(limit=10000)

    if fmt == "json":
        return json.dumps(apps, indent=2, default=str)

    # CSV
    output = io.StringIO()
    if apps:
        writer = csv.DictWriter(output, fieldnames=apps[0].keys())
        writer.writeheader()
        writer.writerows(apps)

    return output.getvalue()
