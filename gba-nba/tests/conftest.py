"""Hermetic test setup: neutralize a developer .env so tests don't depend on local secrets.

A real .env sets INTERNAL_API_KEY, which would 401 the cockpit API tests (written for open/dev
mode). Env vars take precedence over the .env file in pydantic-settings, so forcing it empty here
keeps the suite hermetic regardless of what's on the developer's machine.
"""
from __future__ import annotations

import os

os.environ["INTERNAL_API_KEY"] = ""

from app.core.config import get_settings  # noqa: E402

get_settings.cache_clear()
