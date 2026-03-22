"""Configuration loading from YAML + .env files."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .models import AppConfig


def load_config(config_path: str = "config.yaml", env_path: str = ".env") -> AppConfig:
    """Load application config from YAML file and environment variables.

    Args:
        config_path: Path to YAML config file.
        env_path: Path to .env file for secrets.

    Returns:
        Validated AppConfig instance.

    Raises:
        FileNotFoundError: If config YAML doesn't exist.
        ValueError: If required environment variables are missing.
    """
    # Load .env for secrets
    load_dotenv(env_path)

    # Load YAML config
    config_file = Path(config_path)
    if not config_file.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}\n"
            "Copy config.example.yaml to config.yaml and fill in your details."
        )

    with open(config_file) as f:
        raw = yaml.safe_load(f)

    config = AppConfig(**raw)

    # Validate required env vars
    _check_env_vars()

    return config


def _check_env_vars() -> None:
    """Verify required environment variables are set."""
    required = [
        "BROWSERBASE_API_KEY",
        "BROWSERBASE_PROJECT_ID",
        "EXA_API_KEY",
    ]
    # MODEL_API_KEY can come from several names
    model_key = (
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("MODEL_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or os.environ.get("GOOGLE_GENERATIVE_AI_API_KEY")
    )

    missing = [v for v in required if not os.environ.get(v)]
    if not model_key:
        missing.append("MODEL_API_KEY (or GOOGLE_API_KEY)")

    if missing:
        raise ValueError(
            f"Missing required environment variables: {', '.join(missing)}\n"
            "Copy .env.example to .env and fill in your API keys."
        )


def get_model_api_key() -> str:
    """Get the LLM model API key from environment.

    Checks for Anthropic, Google, and generic MODEL_API_KEY.
    """
    return (
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("MODEL_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or os.environ.get("GOOGLE_GENERATIVE_AI_API_KEY")
        or ""
    )
