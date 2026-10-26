"""Synthetic data and mock providers. Importable only outside production.

Everything in this package is fiction: invented companies, invented prospects, canned web pages and mock models.
Production code never imports it directly; it goes through guarded factories that check `fixtures_allowed`.
Importing any module here in production raises, so a stray import cannot leak fake data into a real workspace.
"""
from app.core.config import get_settings


class FixturesDisabled(RuntimeError):
    pass


if not get_settings().fixtures_allowed:
    raise FixturesDisabled("app.fixtures is disabled when APP_ENV=production")
