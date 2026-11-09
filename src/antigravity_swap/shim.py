"""What the agy shim runs: pick the account for a session, then start it with ``aswap run``.

The sh part of the shim (``shimfile.render``) starts this module with aswap's own Python and puts
the agy it wraps in ASWAP_AGY. agy's own subcommands and --help/--version spend no quota, so they
go straight to that agy. A session (interactive or -p) gets an account first: the decision
``aswap auto`` makes, on quota re-read when the cached reading is too old, then ``aswap run`` on
that account, in this same process.
"""

from __future__ import annotations

import os
import sys
from typing import NoReturn

from antigravity_swap.processes import HELPER_SUBCOMMANDS

PASS_FLAGS = ("-h", "--help", "--version")
DIR_FLAGS = ("--gemini_dir", "-gemini_dir")
VALUE_FLAGS = ("-p", "--print", "--prompt", "-i", "--prompt-interactive", "--model", "--conversation",
               "--output-format", "--log-file") + DIR_FLAGS
UPDATERS = ("update", "install")


def scan(args: list[str]) -> tuple[str | None, bool]:
    """(agy's subcommand or None, whether --help or --version came before it).

    The subcommand is the first argument that is neither a flag nor a flag's value, so in
    ``agy -p update`` the word ``update`` is the prompt, not a subcommand.
    """
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return (args[i + 1] if i + 1 < len(args) else None), False
        if a in PASS_FLAGS:
            return None, True
        if a.startswith("-"):
            i += 2 if a in VALUE_FLAGS else 1
            continue
        return a, False
    return None, False


def split_dir(args: list[str]) -> tuple[str | None, list[str]]:
    """Take ``--gemini_dir`` (``--gemini_dir=D`` or ``--gemini_dir D``) out of ``args``."""
    gdir: str | None = None
    rest: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            rest.extend(args[i:])
            break
        if a in DIR_FLAGS:
            gdir = args[i + 1] if i + 1 < len(args) else gdir
            i += 2
        elif a.startswith(tuple(f + "=" for f in DIR_FLAGS)):
            gdir = a.split("=", 1)[1]
            i += 1
        elif a in VALUE_FLAGS:
            rest.extend(args[i:i + 2])  # a prompt that happens to read "--gemini_dir=x" stays a prompt
            i += 2
        else:
            rest.append(a)
            i += 1
    return gdir, rest


def _cmd(real: str) -> list[str]:
    # ASWAP_AGY may name a .py stand-in, as agy.find_agy allows
    return [sys.executable, real] if real.endswith(".py") else [real]


def _exec(real: str, args: list[str]) -> NoReturn:
    cmd = _cmd(real) + args
    os.execv(cmd[0], cmd)
    raise AssertionError("unreachable")  # os.execv only returns by raising


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    real = os.environ.get("ASWAP_AGY", "")
    if not real:
        print("aswap shim: ASWAP_AGY is not set; this module is started by the agy shim", file=sys.stderr)
        return 2
    word, asked_help = scan(args)
    if word in UPDATERS:
        return _update(real, args)
    if asked_help or word in HELPER_SUBCOMMANDS:
        _exec(real, args)
    return _session(real, args)


def _update(real: str, args: list[str]) -> int:
    """``agy update`` and ``agy install`` may write a new agy over the shim: put it back after."""
    import signal
    import subprocess

    from antigravity_swap import shimfile

    prev = signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl-C belongs to agy
    try:
        rc = subprocess.call(_cmd(real) + args)
    finally:
        signal.signal(signal.SIGINT, prev)
    shimfile.heal(sys.stderr)
    return rc


def _session(real: str, args: list[str]) -> int:
    gdir, rest = split_dir(args)
    if gdir is not None:
        os.environ["ASWAP_GEMINI_DIR"] = gdir  # Manager reads it when built, so set it first

    import time

    from antigravity_swap import auto, cli, paths
    from antigravity_swap.credentials import CredentialError
    from antigravity_swap.failover import pool_for
    from antigravity_swap.fsutil import read_json, write_json
    from antigravity_swap.manager import Manager, with_registry
    from antigravity_swap.store import Registry

    def as_is(why: str | None) -> NoReturn:
        if why:
            print(f"aswap: {why}; starting agy without picking an account", file=sys.stderr)
        _exec(real, args)

    if not Registry.load().accounts:
        as_is(None)

    def refresh(reg, active):
        for a in reg.ordered():  # as failover._next does before it picks
            if a.rotatable() or (active is not None and a.slot == active.slot):
                try:
                    mgr.refresh_usage(reg, a, active=active)  # fetches only when older than usage.max_age_seconds
                except Exception:
                    pass  # a failed read keeps the last-known numbers

    def pick(reg):
        cred = mgr.current_login()
        if cred is None:  # signed out: no login to protect; aswap run picks an account and signs it in
            refresh(reg, None)
            return "signed out", None, None, "", None
        active = reg.find_identity(cred.identity())
        if active is None:
            return "unmanaged", None, None, "", None
        refresh(reg, active)
        last = (read_json(paths.state_file(), default={}) or {}).get("last_switch", 0.0)
        target, reason, _, used = auto.decide(mgr, reg, active, policy, last, time.time())
        return "managed", active, target, reason, used

    try:
        mgr = Manager()
        policy = auto.Policy.from_settings(mgr.settings, pool=pool_for(rest))
        login, active, target, reason, used = with_registry(pick)
    except CredentialError as e:  # the login, or where agy keeps it, cannot be read
        as_is(str(e))
    if login == "unmanaged":
        as_is("agy's current login is not an account aswap manages (add it with: aswap add)")
    if target is not None:
        print(f"aswap: starting on account {target.slot} ({target.label}); account {active.slot} "
              f"is {used:.0f}% used ({reason})", file=sys.stderr)
        if not mgr.isolates:  # aswap run switches the default login: count it for auto's cooldown
            state = read_json(paths.state_file(), default={}) or {}
            write_json(paths.state_file(), {**state, "last_switch": time.time()})
    return cli.main(["run", *([str(target.slot)] if target else []), "--", *rest])


if __name__ == "__main__":
    sys.exit(main())
