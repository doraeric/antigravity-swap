import base64
import json
import time
from pathlib import Path

import pytest

from antigravity_swap.credentials import Credential, FileBackend
from antigravity_swap.manager import Manager
from antigravity_swap.settings import Settings

FAKE = str(Path(__file__).with_name("fake_agy.py"))


def b64(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


def make_cred(email: str, expiry_in: float = 3600, access: str | None = None,
              email_only: bool = False) -> Credential:
    """An agy login. ``email_only`` builds agy 1.4's shape: ``id_token_email``, no ``id_token``."""
    exp = time.strftime("%Y-%m-%dT%H:%M:%S.123456789Z", time.gmtime(time.time() + expiry_in))
    blob = {"token": {"access_token": access or f"at-{email}", "token_type": "Bearer",
                      "refresh_token": f"rt-{email}", "expiry": exp},
            "auth_method": "oauth"}
    if email_only:
        blob["token"]["expires_in"] = int(expiry_in)
        blob["id_token_email"] = email
    else:
        blob["id_token"] = f"{b64({'alg': 'none'})}.{b64({'email': email, 'sub': 'sub-' + email})}.sig"
    return Credential(json.dumps(blob))


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "aswap"
    gdir = tmp_path / "gemini"
    (gdir / "antigravity-cli").mkdir(parents=True)
    plan = tmp_path / "plan.json"
    plan.write_text("{}")
    log = tmp_path / "agy.log"
    log.write_text("")
    monkeypatch.setenv("ASWAP_HOME", str(home))
    monkeypatch.setenv("ASWAP_GEMINI_DIR", str(gdir))
    monkeypatch.setenv("FAKE_AGY_PLAN", str(plan))
    monkeypatch.setenv("FAKE_AGY_LOG", str(log))
    monkeypatch.setenv("ASWAP_AGY", FAKE)
    monkeypatch.setenv("ASWAP_BACKEND", "file")
    from antigravity_swap import manager, style
    from antigravity_swap.usage import UsageError
    monkeypatch.setattr(style, "_enabled", None)

    def offline(token, project=None):
        raise UsageError("network", "tests never reach Google")

    monkeypatch.setattr(manager, "fetch", offline)

    class Env:
        pass

    e = Env()
    e.home, e.gdir, e.plan_path, e.log = home, gdir, plan, log
    e.backend = FileBackend(gdir)

    def set_plan(**kw):
        plan.write_text(json.dumps({k.replace("_at_", "@"): v for k, v in kw.items()}))

    def plan_emails(mapping):
        plan.write_text(json.dumps(mapping))

    def login(email, **kw):
        e.backend.write(make_cred(email, **kw))

    def manager():
        return Manager(settings=Settings(), backend=FileBackend(gdir))

    def calls():
        return [json.loads(x) for x in log.read_text().splitlines() if x.strip()]

    e.set_plan, e.plan, e.login, e.manager, e.calls = set_plan, plan_emails, login, manager, calls
    return e
