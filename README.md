# super-job-apply

Automated job application system that discovers jobs at scale, scores candidate fit, generates ATS-optimized resumes and cover letters per job, and submits applications via cloud browser automation.

Built on the [Browserbase Exa job application template](https://github.com/browserbase/templates/tree/dev/python/exa-browserbase) with significant architectural enhancements. See [Enhancements Over Base Template](#enhancements-over-base-template) for a detailed comparison.

**Requirements:** Python 3.11+, API keys for Anthropic, Browserbase, and Exa.

## How It Works

```
Discover → Score → Tailor → Review → Submit → Track
```

1. **Discover** — Exa AI search finds companies and job postings matching your criteria across the web
2. **Score** — LLM evaluates candidate-job fit (0.0–1.0); jobs below threshold are skipped
3. **Tailor** — Multi-agent pipeline rewrites your resume and generates a cover letter per job
4. **Review** — Interactive CLI lets you approve, skip, or bulk-approve applications
5. **Submit** — Browser agent navigates to each job site, handles login/account creation, fills forms, uploads resume, and clicks submit
6. **Track** — SQLite database records every application with status, materials, and full audit trail

## Key Features

### Multi-Agent Writing Pipeline

Each resume and cover letter passes through three specialized agents:

| Agent | Role |
|---|---|
| **Writer** | Rewrites resume bullets to mirror the job posting's language. Integrates ATS keywords naturally. |
| **Editor** | Reviews for accuracy against the original resume. Flags fabricated claims. Fixes AI-detectable patterns (buzzword density, uniform sentence structure, corporate filler). |
| **Mediator** | Produces the final version. Accuracy flags always override. Editor wins on tone. Writer wins on keyword placement. Logs rationale for every decision. |

All stages are recorded in the audit trail for review.

### Step-by-Step Browser Automation

Uses Stagehand's `act()` API for discrete browser actions instead of autonomous agent mode:

- Click Apply → Handle login/signup → Fill form → Fill remaining fields → Upload resume → Submit
- Each step gets a fresh page handle, avoiding the page-reference crashes common with long-running autonomous agents
- Handles redirects to external ATS systems (Workday, Greenhouse, Lever, iCIMS)
- 98%+ submission success rate in production

### Automated Account Creation & Email Verification

When a job site requires sign-up before applying:

- Prefers "Apply as Guest" or "Quick Apply" when available
- Signs in with configured credentials if an account exists
- Creates a new account if needed
- Detects email verification walls and auto-checks inbox via IMAP
- Opens the verification link in the browser session and resumes the application

### ATS Optimization

- Resumes output as `.docx` (most reliable ATS parsing format)
- Keywords rewritten per job to match posting language
- Match scoring filters out poor-fit applications (research shows targeted applications achieve 3-5x higher interview rates)
- Editor agent prevents keyword stuffing that triggers ATS spam detection

### Cost & Safety Controls

- Configurable model tiers: Sonnet for writing quality, Haiku for browser automation
- Hard cap of 3 attempts per job — prevents runaway retries
- Dead link auto-detection (404s, closed postings, suspended sites)
- Rate limit pauses between browser batches
- Dry-run mode for reviewing materials before submitting

## Quick Start

```bash
git clone https://github.com/youruser/super-job-apply
cd super-job-apply
./setup.sh
```

Then configure:

1. **`.env`** — Add your API keys (see [Required API Keys](#required-api-keys))
2. **`config.yaml`** — Add your candidate profile, skills, and search queries
3. **`resume_template.docx`** — Place your base resume in the project root

Run:

```bash
source .venv/bin/activate
super-job-apply run --dry-run     # Discover, score, tailor (no submissions)
super-job-apply review            # Approve/skip each application
super-job-apply submit            # Submit approved applications
```

## Required API Keys

Configure in `.env` (copy from `.env.example`):

| Variable | Purpose | Source |
|---|---|---|
| `ANTHROPIC_API_KEY` | Writing pipeline, scoring, browser agent | [console.anthropic.com](https://console.anthropic.com) |
| `BROWSERBASE_API_KEY` | Cloud browser sessions | [browserbase.com](https://www.browserbase.com) |
| `BROWSERBASE_PROJECT_ID` | Browserbase project identifier | Browserbase dashboard |
| `EXA_API_KEY` | Job and company discovery | [dashboard.exa.ai](https://dashboard.exa.ai/api-keys) |
| `IMAP_EMAIL` | Optional: email for auto-verification | — |
| `IMAP_APP_PASSWORD` | Optional: email app password | Provider's app password settings |
| `IMAP_SERVER` | Optional: IMAP server hostname | `imap.gmail.com` or `imap.mail.yahoo.com` |

## Configuration

### `config.yaml`

```yaml
candidate:
  name: "Your Name"
  email: "you@email.com"
  account_email: ""          # Optional: separate email for job site accounts
  phone: "+1-555-000-0000"
  linkedin_url: "https://linkedin.com/in/you"
  skills: [Python, Your Skills]
  years_experience: 5
  experience_summary: |
    Your background summary...
  target_roles: ["Software Engineer", "Your Role"]

search:
  queries:
    - "your target role remote jobs"
  num_results_per_query: 10

application:
  min_match_score: 0.6       # Skip jobs scoring below this
  concurrent: false           # Parallel browser sessions (requires Browserbase paid plan)
  max_concurrent_browsers: 1
  use_proxy: false            # Requires Browserbase Developer plan
  dry_run: true               # Set false when ready to submit
  max_retries: 1
  model: "anthropic/claude-haiku-4-5-20251001"        # Stagehand session model
  agent_model: "anthropic/claude-haiku-4-5-20251001"  # Browser form-filling model
  tailoring_model: "claude-sonnet-4-6"                # Resume/cover letter writing model
  account_password: ""        # Password used when creating accounts on job sites
```

### Browserbase Plans

| Feature | Free | Developer+ |
|---|---|---|
| Concurrent sessions | 1 | Up to 25 |
| Proxies / stealth mode | No | Yes |
| Auto CAPTCHA solving | No | Yes |

## CLI Reference

```
super-job-apply [--verbose] COMMAND [OPTIONS]
```

| Command | Description |
|---|---|
| `run [--dry-run] [--config PATH]` | Full pipeline. `--dry-run` stops before browser submission. |
| `review [--config PATH]` | Interactive review of pending applications. Options: `y` approve, `n` skip, `a` approve all, `q` quit. |
| `submit [--config PATH]` | Submit all approved applications via browser. |
| `retry-failed [--config PATH]` | Re-attempt failed applications (respects 3-attempt cap). |
| `stats [--config PATH]` | Display application statistics and response rate recommendations. |
| `recent [-n COUNT] [--config PATH]` | Show recent applications with IDs. |
| `audit JOB_ID [--config PATH]` | Display full audit trail for a job (all writing stages, edits, decisions). |
| `update APP_ID --status STATUS` | Manually update status: `interview`, `rejected`, `offer`. |
| `export [--format csv or json] [-o FILE]` | Export application data. |

## Architecture

```
src/super_job_apply/
├── cli.py                        # Click CLI entry point
├── config.py                     # YAML + .env configuration loader
├── models.py                     # Pydantic data models
├── db.py                         # SQLite persistence layer
├── pipeline.py                   # Pipeline orchestrator
├── audit.py                      # Audit event logging (DB + JSON files)
│
├── discovery/
│   ├── base.py                   # Abstract JobSource interface
│   ├── exa_company.py            # Exa company search → careers pages
│   └── exa_jobs.py               # Exa direct job posting search
│
├── analysis/
│   └── scorer.py                 # LLM-based candidate-job fit scoring
│
├── writers/
│   ├── writer.py                 # Content drafting agent
│   ├── editor.py                 # Accuracy & tone review agent
│   ├── mediator.py               # Conflict resolution agent
│   └── pipeline.py               # Writer → Editor → Mediator orchestration
│
├── tailoring/
│   ├── resume.py                 # Resume rewriting + .docx generation
│   └── cover_letter.py           # Cover letter generation + .docx output
│
├── applicator/
│   ├── browser.py                # Stagehand act() browser automation
│   ├── uploader.py               # Playwright CDP file upload
│   └── email_verifier.py         # IMAP inbox monitoring for verification links
│
└── reporting/
    └── stats.py                  # Statistics, response rates, recommendations
```

### Data Flow

```
config.yaml + .env
       │
       ▼
   Discovery (Exa)
       │
       ▼
   Deduplication (SQLite)
       │
       ▼
   Scoring (Claude)
       │
       ▼
   Filtering (min_match_score)
       │
       ▼
   Tailoring (Writer → Editor → Mediator)
       │
       ▼
   Review (interactive CLI)
       │
       ▼
   Submission (Browserbase + Stagehand act())
       │
       ▼
   Tracking (SQLite + audit JSON files)
```

### Audit Trail

Every application generates structured JSON files in `output/audit/{job_id}/`:

```
output/audit/abc123def456/
├── job_discovered_*.json
├── job_scored_*.json
├── resume_original_*.json
├── resume_writer_draft_*.json
├── resume_editor_review_*.json
├── resume_mediator_final_*.json
├── cover_letter_writer_draft_*.json
├── cover_letter_editor_review_*.json
├── cover_letter_mediator_final_*.json
└── application_submitted_*.json
```

## Enhancements Over Base Template

| Area | Template | super-job-apply |
|---|---|---|
| Resume handling | Same resume sent to every job | Per-job rewrite via 3-agent pipeline |
| Browser automation | `execute()` autonomous agent | `act()` step-by-step (98%+ success) |
| Application tracking | Print summary, forget | SQLite DB with full audit trail |
| Job filtering | Apply to everything | LLM scoring with configurable threshold |
| Account creation | Fails on login walls | Auto sign-up, sign-in, email verification |
| Configuration | Hardcoded in source | YAML + .env, gitignored |
| Deduplication | None | DB-level unique constraint |
| Retry protection | Unlimited | 3-attempt hard cap per job |
| Output format | PDF | .docx (ATS-optimized) |
| Architecture | Single file | Modular package with 13 modules |

## Privacy

All personal data is gitignored:

- `.env` — API keys, IMAP credentials
- `config.yaml` — candidate profile, phone, email, password
- `resume_template.docx` — your resume
- `output/` — tailored documents, audit trails
- `*.db` — application database
- `private/` — backup copies

Only `.env.example`, `config.example.yaml`, and source code are committed.

## Credits

- Base template: [Browserbase Exa Template](https://github.com/browserbase/templates/tree/dev/python/exa-browserbase)
- Browser infrastructure: [Browserbase](https://www.browserbase.com) + [Stagehand](https://docs.stagehand.dev)
- Job discovery: [Exa](https://exa.ai)
- AI: [Anthropic Claude](https://www.anthropic.com)
