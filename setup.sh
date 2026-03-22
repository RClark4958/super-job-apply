#!/bin/bash
# super-job-apply setup script
# Run this after cloning the repository

set -e

echo "=== super-job-apply setup ==="
echo ""

# Check Python version
python3 --version 2>/dev/null || { echo "Error: Python 3.11+ required"; exit 1; }

# Create virtual environment
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

echo "Installing dependencies..."
.venv/bin/pip install -e . -q

echo "Installing Playwright browser..."
.venv/bin/playwright install chromium 2>/dev/null || echo "  (Playwright browser install skipped — not needed for Browserbase cloud)"

# Copy config templates if they don't exist
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo ""
    echo "Created .env — edit it with your API keys:"
    echo "  BROWSERBASE_API_KEY, BROWSERBASE_PROJECT_ID, EXA_API_KEY, ANTHROPIC_API_KEY"
else
    echo ".env already exists — skipping"
fi

if [ ! -f "config.yaml" ]; then
    cp config.example.yaml config.yaml
    echo ""
    echo "Created config.yaml — edit it with your:"
    echo "  - Name, email, phone, LinkedIn"
    echo "  - Skills, experience summary, education"
    echo "  - Target roles and search queries"
else
    echo "config.yaml already exists — skipping"
fi

if [ ! -f "resume_template.docx" ]; then
    echo ""
    echo "NOTE: Place your resume as resume_template.docx in the project root."
    echo "  This file contains your personal data and is gitignored."
fi

# Create private directory for backups
mkdir -p private

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit .env with your API keys"
echo "  2. Edit config.yaml with your profile"
echo "  3. Place your resume as resume_template.docx"
echo "  4. Run: source .venv/bin/activate"
echo "  5. Run: super-job-apply run --dry-run"
echo ""
echo "Commands:"
echo "  super-job-apply run --dry-run    Discover, score, tailor (no submit)"
echo "  super-job-apply review           Approve/skip applications"
echo "  super-job-apply submit           Submit approved applications"
echo "  super-job-apply stats            View statistics"
echo "  super-job-apply recent -n 50     Recent applications with IDs"
