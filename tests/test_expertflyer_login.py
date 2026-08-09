"""Login-layer contract: credential gating and failure modes.

The success path needs a live ExpertFlyer credential, so it is exercised
manually (see skills/expertflyer/references/web-contract.md). Everything that
can run without one is pinned here.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "expertflyer" / "scripts"


def _load(name: str, filename: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


session = _load("expertflyer_session", "expertflyer_session.py")
login = _load("expertflyer_login", "expertflyer_login.py")


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for var in (login.EMAIL_ENV, login.PASSWORD_ENV, session.STATE_ENV):
        monkeypatch.delenv(var, raising=False)


def test_credentials_absent_by_default():
    assert login.credentials_available() is False


def test_both_credentials_required(monkeypatch):
    monkeypatch.setenv(login.EMAIL_ENV, "someone@example.com")
    assert login.credentials_available() is False
    monkeypatch.setenv(login.PASSWORD_ENV, "secret")
    assert login.credentials_available() is True


def test_missing_credentials_raise_auth_error(monkeypatch):
    monkeypatch.setenv(login.EMAIL_ENV, "someone@example.com")
    with pytest.raises(session.AuthError):
        login._credentials()


def test_credentials_are_not_echoed_in_the_error(monkeypatch):
    monkeypatch.setenv(login.EMAIL_ENV, "someone@example.com")
    with pytest.raises(session.AuthError) as excinfo:
        login._credentials()
    assert "someone@example.com" not in str(excinfo.value)


def test_login_failure_is_an_auth_error_not_a_bare_runtime_error():
    """Callers catch AuthError; a RuntimeError would escape as a traceback."""
    assert issubclass(session.AuthError, session.ExpertFlyerError)
    assert isinstance(login._auth_error("nope"), session.AuthError)
    assert login._auth_error("nope").kind == "auth"


def test_blocked_and_auth_are_distinct_kinds():
    """403 means the bot wall; a redirect to Auth0 means an expired session."""
    assert session.AuthError("x").kind == "auth"
    assert session.BlockedError("x").kind == "blocked"
    assert session.AuthError("x").kind != session.BlockedError("x").kind


def test_unset_state_env_is_actionable(monkeypatch):
    with pytest.raises(session.AuthError) as excinfo:
        session.load_storage_state()
    detail = str(excinfo.value)
    assert session.STATE_ENV in detail
    assert login.EMAIL_ENV in detail


def test_missing_state_file_is_actionable(monkeypatch, tmp_path):
    monkeypatch.setenv(session.STATE_ENV, str(tmp_path / "absent.json"))
    with pytest.raises(session.AuthError, match="does not exist"):
        session.load_storage_state()


def test_invalid_state_file_is_rejected(monkeypatch, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("not json at all")
    monkeypatch.setenv(session.STATE_ENV, str(bad))
    with pytest.raises(session.AuthError, match="not valid JSON"):
        session.load_storage_state()


def test_cookieless_state_file_is_rejected(monkeypatch, tmp_path):
    empty = tmp_path / "empty.json"
    empty.write_text('{"cookies": []}')
    monkeypatch.setenv(session.STATE_ENV, str(empty))
    with pytest.raises(session.AuthError, match="no cookies"):
        session.load_storage_state()


def test_valid_state_file_loads(monkeypatch, tmp_path):
    good = tmp_path / "good.json"
    good.write_text('{"cookies": [{"name": "__session__0", "value": "x"}]}')
    monkeypatch.setenv(session.STATE_ENV, str(good))
    assert session.load_storage_state()["cookies"][0]["name"] == "__session__0"
