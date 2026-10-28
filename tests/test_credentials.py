"""agy's login: identity in both formats, and "signed out" told apart from "unreadable"."""

import json
import os
import re
import subprocess

import pytest

from antigravity_swap.cli import main
from antigravity_swap.credentials import (Credential, CredentialAccessError, CredentialFormatError,
                                          CredentialSchemaError, FileBackend, MacKeychainBackend,
                                          SecretServiceBackend, default_backend)
from antigravity_swap.manager import Manager
from antigravity_swap.store import Registry, load_cred

from conftest import make_cred


def test_identity_from_agy_14_id_token_email():
    c = make_cred("a@x.io", email_only=True)
    assert "id_token" not in c.data
    assert (c.identity().email, c.identity().sub) == ("a@x.io", None)


def test_identity_prefers_id_token_claims():
    blob = json.loads(make_cred("a@x.io").raw)
    blob["id_token_email"] = "other@x.io"
    assert Credential(json.dumps(blob)).identity().email == "a@x.io"


def test_not_json_is_a_format_error():
    with pytest.raises(CredentialFormatError):
        Credential("{truncated")


@pytest.mark.parametrize("raw", ["[]", '{"auth_method": "oauth"}', '{"token": "flat"}'])
def test_json_of_another_shape_is_a_schema_error(raw):
    with pytest.raises(CredentialSchemaError):
        Credential(raw)


def test_schema_error_names_keys_never_values():
    with pytest.raises(CredentialSchemaError) as e:
        Credential(json.dumps({"tok": {"access_token": "SECRET"}, "auth_method": "oauth"}))
    assert "top-level keys: auth_method, tok" in str(e.value)
    assert "SECRET" not in str(e.value)


def test_current_login_tells_missing_from_unreadable(env):
    mgr = env.manager()
    where = re.escape(str(env.backend.path))
    assert mgr.current_login() is None
    env.backend.path.write_text("{truncated")
    with pytest.raises(CredentialFormatError, match=where):
        mgr.current_login()
    env.backend.path.write_text('{"auth_method": "oauth"}')
    with pytest.raises(CredentialSchemaError, match=where):
        mgr.current_login()


def test_file_backend_tells_absent_from_unreadable(tmp_path):
    fb = FileBackend(tmp_path)
    assert fb.read() is None  # no file: signed out
    fb.path.parent.mkdir(parents=True)
    fb.path.mkdir()  # there, but not readable as a file
    with pytest.raises(CredentialAccessError, match="cannot read the file"):
        fb.read()
    fb.path.rmdir()
    fb.path.write_text("  \n")
    with pytest.raises(CredentialFormatError, match="empty"):
        fb.read()
    fb.path.write_bytes(b"\xff\xfe\x00garbage")
    with pytest.raises(CredentialFormatError, match="UTF-8"):
        fb.read()


def tool(monkeypatch, rc=0, out="", err="", missing=False):
    """Stand in for secret-tool / security with what they really print (probed on Linux)."""
    from antigravity_swap import credentials

    def run(argv, **kw):
        if missing:
            raise FileNotFoundError(2, "No such file or directory")
        return subprocess.CompletedProcess(argv, rc, out, err)
    monkeypatch.setattr(credentials.subprocess, "run", run)


def test_secret_tool_nothing_found_is_signed_out(monkeypatch):
    tool(monkeypatch, rc=1)  # no match: exit 1, silent
    assert SecretServiceBackend().read() is None


def test_secret_tool_failure_is_reported_with_its_own_message(monkeypatch):
    tool(monkeypatch, rc=1, err="secret-tool: Cannot autolaunch D-Bus without X11 $DISPLAY\n")
    with pytest.raises(CredentialAccessError, match=r"Cannot autolaunch D-Bus without X11 \$DISPLAY"):
        SecretServiceBackend().read()


def test_missing_tool_is_an_access_error(monkeypatch):
    tool(monkeypatch, missing=True)
    with pytest.raises(CredentialAccessError, match="cannot run secret-tool"):
        SecretServiceBackend().read()
    with pytest.raises(CredentialAccessError, match="cannot run security"):
        MacKeychainBackend().read()


def test_empty_keyring_value_is_a_format_error(monkeypatch):
    tool(monkeypatch, rc=0, out="\n")
    with pytest.raises(CredentialFormatError, match="empty"):
        SecretServiceBackend().read()


def test_keychain_item_not_found_is_signed_out(monkeypatch):
    tool(monkeypatch, rc=44, err="security: SecKeychainSearchCopyNext: The specified item could not be found.")
    assert MacKeychainBackend().read() is None


def test_keychain_failure_is_reported_with_exit_and_message(monkeypatch):
    tool(monkeypatch, rc=36, err="security: User interaction is not allowed.")
    with pytest.raises(CredentialAccessError, match=r"exit 36\): security: User interaction is not allowed"):
        MacKeychainBackend().read()


def test_keychain_bad_go_keyring_payload_is_a_format_error(monkeypatch):
    tool(monkeypatch, rc=0, out="go-keyring-base64:not*base64\n")
    with pytest.raises(CredentialFormatError, match="base64"):
        MacKeychainBackend().read()


@pytest.mark.skipif(os.name != "nt", reason="Credential Manager is Windows-only")
def test_wincred_absent_is_signed_out():
    from antigravity_swap.credentials import WindowsBackend
    assert WindowsBackend(target="antigravity-swap-test:absent").read() is None


@pytest.mark.parametrize("cls,on_mac", [(SecretServiceBackend, False), (MacKeychainBackend, True)])
@pytest.mark.parametrize("err", [CredentialFormatError("login is not valid JSON (test)"),
                                 CredentialAccessError("secret-tool lookup failed: keyring is locked (test)")])
def test_detection_keeps_a_keyring_it_cannot_use(env, monkeypatch, cls, on_mac, err):
    from antigravity_swap import credentials
    monkeypatch.delenv("ASWAP_BACKEND")
    monkeypatch.setattr(credentials, "IS_WINDOWS", False)
    monkeypatch.setattr(credentials, "IS_MACOS", on_mac)
    monkeypatch.setattr(credentials.shutil, "which", lambda name: "/usr/bin/" + name)

    def unusable(self):
        raise err
    monkeypatch.setattr(cls, "read", unusable)
    assert isinstance(default_backend(env.gdir), cls)
    with pytest.raises(type(err), match="(keyring|Keychain).*: " + re.escape(str(err))):
        Manager().current_login()


def test_add_shows_the_keyring_error_not_signed_out(env, monkeypatch, capsys):
    monkeypatch.setenv("ASWAP_BACKEND", "secret-service")
    tool(monkeypatch, rc=1, err="secret-tool: Cannot autolaunch D-Bus without X11 $DISPLAY\n")
    assert main(["add"]) == 1
    err = capsys.readouterr().err
    assert "Cannot autolaunch D-Bus" in err and "not signed in" not in err


def test_add_reports_each_failure_differently(env, capsys):
    assert main(["add"]) == 1
    assert "not signed in" in capsys.readouterr().err
    env.backend.path.write_text("{truncated")
    assert main(["add"]) == 1
    assert "not valid JSON" in capsys.readouterr().err
    env.backend.path.write_text('{"auth_method": "oauth"}')
    assert main(["add"]) == 1
    assert 'no "token" object' in capsys.readouterr().err
    env.backend.path.write_text(json.dumps({"token": {"access_token": "a"}, "auth_method": "oauth"}))
    assert main(["add"]) == 1
    assert "neither id_token nor id_token_email" in capsys.readouterr().err
    assert not Registry.load().accounts


def test_agy_14_logins_add_and_switch(env):
    env.plan({"a@x.io": "ok", "b@x.io": "ok"})
    env.login("a@x.io", email_only=True)
    assert main(["add", "--alias", "alt-01"]) == 0
    env.login("b@x.io", email_only=True)
    assert main(["add"]) == 0
    assert [(a.slot, a.email) for a in Registry.load().ordered()] == [(1, "a@x.io"), (2, "b@x.io")]

    assert main(["switch", "alt-01"]) == 0
    assert env.backend.read().identity().email == "a@x.io"
    assert load_cred(2).identity().email == "b@x.io"  # the login it left is kept
    assert "id_token_email" in json.loads(load_cred(1).raw)  # the blob travels whole


def test_switch_succeeds_when_the_read_back_fails(env, monkeypatch):
    from antigravity_swap import agy
    from test_core import add_accounts
    mgr, reg = add_accounts(env, "a@x.io", "b@x.io")

    def check(cmd, gdir, timeout=60):
        env.backend.path.write_text("{truncated")  # agy left a login aswap cannot read back
        return agy.Check(True, "ok")
    monkeypatch.setattr(agy, "check_login", check)
    res = mgr.switch(reg, reg.get(1))
    assert res.switched and res.verified
    assert mgr.read_back() is None
    with pytest.raises(CredentialFormatError):  # the next real read still says what is wrong
        mgr.current_login()


def test_auto_reports_unreadable_login(env):
    from antigravity_swap import auto
    env.backend.path.write_text("{truncated")
    t = auto.tick(env.manager(), auto.Policy())
    assert t.outcome == auto.Outcome.ERROR
    assert t.events[-1]["event"] == "error" and "not valid JSON" in t.events[-1]["message"]
