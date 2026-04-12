"""Whitelist of trusted job application domains.

Only jobs hosted on these domains will be submitted. This prevents
wasting browser sessions and API credits on spam/fake job aggregators.
"""

from urllib.parse import urlparse

# ATS platforms — these host real application forms
ATS_PLATFORMS = {
    "boards.greenhouse.io", "job-boards.greenhouse.io",
    "jobs.lever.co", "api.lever.co",
    "apply.workable.com",
    "jobs.ashbyhq.com",
    "jobs.smartrecruiters.com", "careers.smartrecruiters.com",
    "jobs.jobvite.com",
    "icims.com",  # matches *.icims.com
    "myworkdayjobs.com",  # matches *.myworkdayjobs.com
    "taleo.net",  # matches *.taleo.net
    "teamtailor.com",  # matches *.teamtailor.com
}

# Direct company career sites — known legitimate employers
COMPANY_DOMAINS = {
    # Big tech
    "jobs.apple.com", "careers.google.com", "careers.microsoft.com",
    "amazon.jobs", "meta.com", "facebook.com",

    # AI/ML companies
    "anthropic.com", "openai.com", "databricks.com",
    "snowflake.com", "stripe.com", "discord.com",
    "notion.so", "figma.com", "cloudflare.com",
    "vercel.com", "supabase.com", "render.com",
    "anyscale.com", "modal.com", "langchain.com",
    "pinecone.io", "weaviate.io", "cohere.com",
    "huggingface.co", "wandb.ai", "deepmind.com",
    "scale.com", "cursor.com", "mistral.ai",
    "replicate.com",

    # Data/DevOps/Cloud
    "datadoghq.com", "grafana.com", "elastic.co",
    "confluent.io", "hashicorp.com", "cockroachlabs.com",
    "planetscale.com", "fivetran.com", "getdbt.com",
    "prefect.io", "dagster.io", "airbyte.com",
    "neon.tech", "timescale.com", "clickhouse.com",
    "mongodb.com", "redis.com", "fly.io", "railway.app",
    "temporal.io", "pulumi.com",

    # Developer tools / SaaS
    "retool.com", "linear.app", "posthog.com",
    "about.gitlab.com", "gitlab.com",
    "snyk.io", "launchdarkly.com", "sentry.io",
    "sourcegraph.com", "tailscale.com",

    # Enterprise/Healthcare/Finance
    "unitedhealthgroup.com", "molinahealthcare.com",
    "capitalonecareers.com", "goldmansachs.com",
    "jpmorgan.com", "wellsfargo.com",
    "boozallen.com", "leidos.com", "peraton.com",
    "lockheedmartinjobs.com", "raytheon.com",
    "stanford.edu", "mit.edu",

    # Job platforms with real application forms
    "builtin.com", "builtinsf.com", "builtinnyc.com",
    "weworkremotely.com", "4dayweek.io",
    "wellfound.com", "angel.co",
    "simplify.jobs", "otta.com",

    # Staffing with real forms
    "talantix.io", "crossover.com",
    "toptal.com", "turing.com",
    "n-ix.com", "teksystems.com",

    # Job boards / aggregators with working apply forms
    "getonbrd.com", "jaabz.com",
    "remoterocketship.com", "dailyremote.com",

    # Additional company career sites
    "cognizant.com",

    # Other known companies
    "airbnb.com", "uber.com", "lyft.com",
    "doordash.com", "instacart.com", "robinhood.com",
    "plaid.com", "brex.com", "ramp.com",
    "rippling.com", "gusto.com", "lattice.com",
    "airtable.com", "asana.com", "monday.com",
    "twilio.com", "sendgrid.com", "segment.com",
    "pagerduty.com", "splunk.com", "crowdstrike.com",
    "okta.com", "auth0.com", "1password.com",
}


def is_whitelisted(url: str) -> bool:
    """Check if a URL's domain is on the whitelist."""
    hostname = (urlparse(url).hostname or "").replace("www.", "").lower()

    # Exact match against ATS platforms
    if hostname in ATS_PLATFORMS:
        return True

    # Subdomain match for ATS (e.g., careers-captrust.icims.com)
    for ats in ATS_PLATFORMS:
        if hostname.endswith(f".{ats}") or hostname.endswith(ats):
            return True

    # Match company domains (including subdomains like careers.airbnb.com)
    for company in COMPANY_DOMAINS:
        if hostname == company or hostname.endswith(f".{company}"):
            return True
        # Also match if the company name is in the hostname
        # e.g., careers.datadoghq.com matches datadoghq.com
        company_base = company.split(".")[0]
        if len(company_base) > 4 and company_base in hostname:
            return True

    return False
