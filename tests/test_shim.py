"""The agy shim: argument routing, the account it starts on, and the script on disk."""

import io
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import FAKE, make_cred
from test_core import add_accounts, put_usage, reading

from antigravity_swap import agy, cli, paths, shim, shimfile
from antigravity_swap.credentials import FileBackend
from antigravity_swap.fsutil import read_json
from antigravity_swap.manager import Manager

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the shim is a sh script; no Windows support")
needs_perms = pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                                 reason="root ignores directory permissions")


# --- routing -----------------------------------------------------------------------------


@pytest.mark.parametrize("args,word,asked_help", [
    (["models"], "models", False),
    (["--gemini_dir=/d", "mcp", "list"], "mcp", False),
    (["--gemini_dir", "/d", "mcp"], "mcp", False),
    (["-p", "update"], None, False),  # the prompt, not the subcommand
    (["--model", "m", "-c"], None, False),
    (["--help"], None, True),
    (["-p", "--help"], None, False),  # a prompt that reads --help
    (["update"], "update", False),
    ([], None, False),
])
def test_scan(args, word, asked_help):
    assert shim.scan(args) == (word, asked_help)


@pytest.mark.parametrize("args,gdir,rest", [
    (["--gemini_dir=/d", "-c"], "/d", ["-c"]),
    (["-c", "--gemini_dir", "/d"], "/d", ["-c"]),
    (["-gemini_dir=/d"], "/d", []),
    (["-p", "--gemini_dir=/d"], None, ["-p", "--gemini_dir=/d"]),  # that is the prompt
    (["-c"], None, ["-c"]),
])
def test_split_dir(args, gdir, rest):
    assert shim.split_dir(args) == (gdir, rest)


class Exec(Exception):
    pass


@pytest.fixture
def execs(monkeypatch):
    """Commands the shim exec'd into, instead of exec'ing."""
    seen = []

    def fake(path, argv):
        seen.append(argv)
        raise Exec()

    monkeypatch.setattr(shim.os, "execv", fake)
    return seen


@pytest.fixture
def runs(monkeypatch):
    """Arguments the shim passed to aswap's CLI, instead of running it."""
    seen = []
    monkeypatch.setattr(cli, "main", lambda argv: seen.append(list(argv)) or 0)
    return seen


def test_helpers_go_straight_to_agy(env, execs, runs):
    with pytest.raises(Exec):
        shim.main(["--gemini_dir=/d", "mcp", "list"])
    assert execs == [[sys.executable, FAKE, "--gemini_dir=/d", "mcp", "list"]] and runs == []


def test_update_runs_agy_then_puts_the_shim_back(env, monkeypatch):
    calls, healed = [], []
    monkeypatch.setattr(subprocess, "call", lambda cmd: calls.append(cmd) or 0)
    monkeypatch.setattr(shimfile, "heal", lambda err: healed.append(True))
    assert shim.main(["update"]) == 0
    assert calls == [[sys.executable, FAKE, "update"]] and healed == [True]


def test_no_accounts_starts_agy_as_is(env, execs, runs):
    with pytest.raises(Exec):
        shim.main(["-c"])
    assert execs == [[sys.executable, FAKE, "-c"]] and runs == []


def test_unmanaged_login_starts_agy_as_is(env, execs, runs, capsys):
    add_accounts(env, "a@x.io")
    env.login("z@x.io")
    with pytest.raises(Exec):
        shim.main([f"--gemini_dir={env.gdir}", "-c"])
    assert execs == [[sys.executable, FAKE, f"--gemini_dir={env.gdir}", "-c"]] and runs == []
    assert "not an account aswap manages" in capsys.readouterr().err


def test_signed_out_lets_run_pick_an_account(env, execs, runs):
    add_accounts(env, "a@x.io", "b@x.io")
    env.backend.path.unlink()  # agy is not signed in at all
    assert shim.main(["-c"]) == 0
    assert runs == [["run", "--", "-c"]] and execs == []


def test_signed_out_session_runs_on_a_stored_account(env):
    env.plan({"a@x.io": "ok", "b@x.io": "ok"})
    add_accounts(env, "a@x.io", "b@x.io")
    env.backend.path.unlink()
    assert shim.main(["-p", "hi"]) == 0
    last = env.calls()[-1]
    assert last["email"] == "a@x.io" and last["args"][:2] == ["-p", "hi"]


@pytest.mark.parametrize("breakage", ["unreadable login", "bad backend"])
def test_a_login_aswap_cannot_read_starts_agy_as_is(env, execs, runs, capsys, monkeypatch, breakage):
    add_accounts(env, "a@x.io")
    if breakage == "unreadable login":
        (env.gdir / "antigravity-cli" / "antigravity-oauth-token").write_text("not json")
    else:
        monkeypatch.setenv("ASWAP_BACKEND", "nonsense")
    with pytest.raises(Exec):
        shim.main(["-c"])
    assert execs == [[sys.executable, FAKE, "-c"]] and runs == []
    assert "starting agy without picking an account" in capsys.readouterr().err


# --- the account it starts on ------------------------------------------------------------


def test_over_threshold_starts_on_the_better_account(env, runs, capsys):
    add_accounts(env, "a@x.io", "b@x.io")  # b is agy's current login
    put_usage(1, reading(g5=10))
    put_usage(2, reading(g5=95))
    assert shim.main(["-p", "fix it"]) == 0
    assert runs == [["run", "1", "--", "-p", "fix it"]]
    assert "starting on account 1" in capsys.readouterr().err


def test_under_threshold_leaves_the_choice_to_run(env, runs):
    add_accounts(env, "a@x.io", "b@x.io")
    put_usage(1, reading(g5=10))
    put_usage(2, reading(g5=50))
    assert shim.main(["-c"]) == 0
    assert runs == [["run", "--", "-c"]]


def test_the_model_picks_the_quota_pool(env, runs):
    add_accounts(env, "a@x.io", "b@x.io")
    put_usage(1, reading(g5=10, p5=10))
    put_usage(2, reading(g5=10, p5=95))
    shim.main(["--model", "claude-sonnet-4-6"])
    assert runs == [["run", "1", "--", "--model", "claude-sonnet-4-6"]]


def test_stale_readings_are_refreshed_first(env, runs, monkeypatch):
    from antigravity_swap import manager

    add_accounts(env, "a@x.io", "b@x.io")
    old = reading(g5=10)
    old["ts"] = time.time() - 3600  # a looked free an hour ago
    put_usage(1, old)
    put_usage(2, reading(g5=95))
    fetched = []

    def fetch(token, project=None):
        fetched.append(token)
        return reading(g5=97)

    monkeypatch.setattr(manager, "fetch", fetch)
    shim.main(["-c"])
    assert fetched == ["at-a@x.io"]  # only the stale reading went to the network
    assert runs == [["run", "--", "-c"]]  # a is at 97% now too, so nobody is better


def test_cli_gemini_dir_is_used_as_aswap_gemini_dir(env, tmp_path, monkeypatch):
    add_accounts(env, "a@x.io", "b@x.io")
    other = tmp_path / "other"
    FileBackend(other).write(make_cred("a@x.io"))
    put_usage(1, reading(g5=10))
    put_usage(2, reading(g5=10))
    seen = {}
    monkeypatch.setattr(cli, "main", lambda argv: seen.update(argv=argv, gdir=os.environ["ASWAP_GEMINI_DIR"]) or 0)
    shim.main([f"--gemini_dir={other}", "-c"])
    assert seen == {"argv": ["run", "--", "-c"], "gdir": str(other)}


def test_switching_the_default_login_counts_for_the_cooldown(env, runs, monkeypatch):
    add_accounts(env, "a@x.io", "b@x.io")
    put_usage(1, reading(g5=10))
    put_usage(2, reading(g5=95))
    monkeypatch.setattr(Manager, "isolates", property(lambda self: False))
    shim.main(["-c"])
    assert time.time() - read_json(paths.state_file())["last_switch"] < 60


# --- the script on disk ------------------------------------------------------------------


@pytest.fixture
def bindir(tmp_path, monkeypatch):
    """A PATH whose first agy is a script in its own directory."""
    d = tmp_path / "bin"
    d.mkdir()
    real = d / "agy"
    real.write_text('#!/bin/sh\necho "real agy $*"\n')
    real.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ASWAP_HOME", str(tmp_path / "aswap"))
    monkeypatch.setenv("ASWAP_BACKEND", "file")
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}/usr/bin{os.pathsep}/bin")
    for var in ("ASWAP_AGY", "ASWAP_GEMINI_DIR", "VIRTUAL_ENV", "CONDA_PREFIX"):
        monkeypatch.delenv(var, raising=False)
    return d


def test_install_in_place(bindir):
    out = io.StringIO()
    rec = shimfile.install(out)
    assert (rec.mode, rec.shim, rec.real) == (shimfile.IN_PLACE, str(bindir / "agy"), str(bindir / "agy.real"))
    assert shimfile.is_shim(bindir / "agy") and "real agy" in (bindir / "agy.real").read_text()
    assert shimfile.load_record() == rec
    assert "export PATH" not in out.getvalue()


def test_install_never_overwrites_agy_real(bindir):
    (bindir / "agy.real").write_text("something else")
    with pytest.raises(shimfile.ShimError, match="already exists"):
        shimfile.install(io.StringIO())
    assert not shimfile.is_shim(bindir / "agy") and shimfile.load_record() is None


def test_install_twice_is_refused(bindir):
    shimfile.install(io.StringIO())
    with pytest.raises(shimfile.ShimError, match="already installed"):
        shimfile.install(io.StringIO())


@needs_perms
def test_install_ahead_on_path_when_the_agy_directory_is_read_only(bindir, tmp_path, monkeypatch):
    early = tmp_path / "early"
    early.mkdir()
    monkeypatch.setenv("PATH", f"{early}{os.pathsep}{bindir}")
    bindir.chmod(0o555)
    try:
        rec = shimfile.install(io.StringIO())
    finally:
        bindir.chmod(0o755)
    assert (rec.mode, rec.shim, rec.real) == (shimfile.SEPARATE, str(early / "agy"), str(bindir / "agy"))
    assert "real agy" in (bindir / "agy").read_text()  # the original is untouched


@needs_perms
def test_install_falls_back_to_local_bin_and_says_how_to_reach_it(bindir, tmp_path, monkeypatch):
    shims = tmp_path / "pyenv" / "shims"  # regenerated by its own tool, so never used
    shims.mkdir(parents=True)
    monkeypatch.setenv("PATH", f"{shims}{os.pathsep}{bindir}")
    bindir.chmod(0o555)
    out = io.StringIO()
    try:
        rec = shimfile.install(out)
    finally:
        bindir.chmod(0o755)
    local = Path(os.environ["HOME"]) / ".local" / "bin"
    assert rec.shim == str(local / "agy") and shimfile.is_shim(local / "agy")
    assert f'export PATH="{local}:$PATH"' in out.getvalue()


def test_uninstall_puts_the_original_back(bindir):
    shimfile.install(io.StringIO())
    shimfile.uninstall(io.StringIO())
    assert "real agy" in (bindir / "agy").read_text()
    assert not (bindir / "agy.real").exists() and shimfile.load_record() is None


def test_uninstall_leaves_a_replaced_shim_alone(bindir):
    shimfile.install(io.StringIO())
    (bindir / "agy").write_text("new agy")
    out = io.StringIO()
    shimfile.uninstall(out)
    assert (bindir / "agy").read_text() == "new agy" and (bindir / "agy.real").exists()
    assert "left it as it is" in out.getvalue() and shimfile.load_record() is None


def test_heal_takes_the_update_and_puts_the_shim_back(bindir):
    shimfile.install(io.StringIO())
    (bindir / "agy").write_text('#!/bin/sh\necho "new agy"\n')  # an agy update wrote over the shim
    err = io.StringIO()
    shimfile.heal(err)
    assert shimfile.is_shim(bindir / "agy") and "new agy" in (bindir / "agy.real").read_text()
    assert sorted(p.name for p in bindir.iterdir()) == ["agy", "agy.real"]  # no backup is kept
    assert "put the shim back" in err.getvalue()


def test_heal_leaves_a_good_shim_alone(bindir):
    shimfile.install(io.StringIO())
    before = (bindir / "agy").stat().st_ino
    err = io.StringIO()
    shimfile.heal(err)
    assert (bindir / "agy").stat().st_ino == before and err.getvalue() == ""


def test_heal_rewrites_an_old_shim_and_a_lost_python(bindir):
    rec = shimfile.install(io.StringIO())
    rec.python = "/gone/python"
    shimfile.save_record(rec)
    old = shimfile.render(rec.python, rec.real).replace(f"shim v{shimfile.VERSION}", "shim v0")
    (bindir / "agy").write_text(old)
    shimfile.heal(io.StringIO())
    text = (bindir / "agy").read_text()
    assert shimfile.shim_version(bindir / "agy") == shimfile.VERSION and sys.executable in text
    assert shimfile.load_record().python == sys.executable


def test_heal_does_not_touch_a_replaced_shim_in_its_own_directory(bindir, tmp_path):
    early = tmp_path / "early"
    early.mkdir()
    shimfile._write(early / "agy", shimfile.render(sys.executable, str(bindir / "agy")))
    shimfile.save_record(shimfile.Record(str(early / "agy"), shimfile.SEPARATE, str(bindir / "agy"), sys.executable))
    (early / "agy").write_text("someone else's agy")
    err = io.StringIO()
    shimfile.heal(err)
    assert (early / "agy").read_text() == "someone else's agy" and "no longer aswap's shim" in err.getvalue()


def test_find_agy_uses_the_agy_behind_the_shim_and_never_a_stale_one(bindir):
    shimfile.install(io.StringIO())
    assert agy.find_agy() == [str(bindir / "agy.real")]
    (bindir / "agy").write_text("#!/bin/sh\n")  # replaced, and not put back yet
    assert agy.find_agy() == [str(bindir / "agy")]


def test_find_agy_skips_a_shim_on_path(bindir, tmp_path, monkeypatch):
    early = tmp_path / "early"
    early.mkdir()
    shimfile._write(early / "agy", shimfile.render(sys.executable, "/somewhere/agy"))
    monkeypatch.setenv("PATH", f"{early}{os.pathsep}{bindir}")
    assert agy.find_agy() == [str(bindir / "agy")]


def test_script_hands_over_to_python_with_the_real_agy(tmp_path):
    py = tmp_path / "py"
    py.write_text('#!/bin/sh\necho "py ASWAP_AGY=$ASWAP_AGY $*"\n')
    py.chmod(0o755)
    script = tmp_path / "agy"
    shimfile._write(script, shimfile.render(str(py), "/the/real agy"))
    out = subprocess.run([str(script), "-p", "a b"], capture_output=True, text=True).stdout
    assert out == "py ASWAP_AGY=/the/real agy -I -m antigravity_swap.shim -p a b\n"


def test_script_runs_agy_directly_when_python_is_gone(tmp_path):
    real = tmp_path / "real agy"
    real.write_text('#!/bin/sh\necho "real $*"\n')
    real.chmod(0o755)
    script = tmp_path / "agy"
    shimfile._write(script, shimfile.render(str(tmp_path / "no-python"), str(real)))
    r = subprocess.run([str(script), "models"], capture_output=True, text=True)
    assert r.stdout == "real models\n" and "not found" in r.stderr


# --- through the CLI ---------------------------------------------------------------------


def test_cli_install_status_uninstall(bindir, capsys):
    assert cli.main(["shim", "install"]) == 0
    assert cli.main(["shim"]) == 0
    assert "(in place)" in capsys.readouterr().out
    assert cli.main(["shim", "uninstall"]) == 0
    assert "real agy" in (bindir / "agy").read_text()


@pytest.mark.parametrize("command", [["config", "path"], ["shim"]])
def test_any_command_puts_a_replaced_shim_back(bindir, command):
    shimfile.install(io.StringIO())
    (bindir / "agy").write_text('#!/bin/sh\necho "new agy"\n')
    assert cli.main(command) == 0
    assert shimfile.is_shim(bindir / "agy") and "new agy" in (bindir / "agy.real").read_text()


def test_purge_takes_the_shim_out_first(bindir):
    shimfile.install(io.StringIO())
    assert cli.main(["purge", "--yes"]) == 0
    assert "real agy" in (bindir / "agy").read_text() and not (bindir / "agy.real").exists()
