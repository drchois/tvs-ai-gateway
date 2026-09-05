from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values


def load_environment(root: Path | None = None) -> str:
    """Load common .env and then .env.<APP_ENV>; process env always has priority."""
    project_root = root or Path.cwd()
    process_keys = set(os.environ)
    common = dotenv_values(project_root / ".env")
    for key, value in common.items():
        if value is not None and key not in process_keys:
            os.environ[key] = value

    app_env = (os.getenv("APP_ENV") or "dev").strip().lower() or "dev"
    profile = dotenv_values(project_root / f".env.{app_env}")
    for key, value in profile.items():
        if value is not None and key not in process_keys:
            os.environ[key] = value
    return app_env
