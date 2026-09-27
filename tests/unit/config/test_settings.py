"""Smoke tests for k-equity-research."""

from src.config.settings import settings


def test_settings_initialization() -> None:
    """Verify settings can be loaded with default values."""
    assert settings.app_name == "k-equity-research"
    assert settings.env == "development"
    assert not settings.debug
