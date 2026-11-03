"""Settings read real env vars with the same clean-up as .env lines (found when a Docker --env-file key had quotes)."""
import pytest

from app import config


@pytest.mark.parametrize("raw", ['abc123', ' abc123', '"abc123"', ' "abc123" ', "'abc123'", "abc123\r", ' "abc123"\r\n'])
def test_env_value_is_cleaned(monkeypatch, raw):
    monkeypatch.setenv("SOME_TEST_KEY", raw)
    assert config._get("SOME_TEST_KEY") == "abc123"


def test_blank_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("SOME_TEST_KEY", '  ""  ')
    assert config._get("SOME_TEST_KEY", "fallback") == "fallback"


def test_inner_quotes_and_spaces_are_kept(monkeypatch):
    monkeypatch.setenv("SOME_TEST_KEY", 'a "b" c')
    assert config._get("SOME_TEST_KEY") == 'a "b" c'
