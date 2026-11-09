"""The agy shim on disk: install it, recognise it, put it back after an update, remove it.

``aswap shim install`` puts a small sh script named ``agy`` where a plain ``agy`` finds it first.
The script runs ``antigravity_swap.shim`` with aswap's own Python, which picks the account and
starts the real agy. If that Python is gone, the script starts the real agy directly.

Where the script went and which agy it starts are recorded in ``shim.json`` in aswap's data
folder. The script also carries a marker line, so aswap recognises it even without the record.
"""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TextIO

from antigravity_swap import paths
from antigravity_swap.fsutil import locked, read_json, write_json

MARKER = "# antigravity-swap shim"
VERSION = 1
REAL_NAME = "agy.real"
IN_PLACE = "in-place"  # the original agy was renamed to agy.real and the shim took its name
SEPARATE = "separate"  # the shim has its own directory, earlier on PATH; the original is untouched
_MARKER_RE = re.compile(r"^# antigravity-swap shim v(\d+)", re.M)


class ShimError(Exception):
    pass


@dataclass
class Record:
    shim: str    # where the shim script is
    mode: str    # IN_PLACE or SEPARATE
    real: str    # the agy the shim starts
    python: str  # the Python the shim runs aswap with
    version: int = VERSION


# --- recognising and writing the script --------------------------------------------------


def shim_version(path) -> int | None:
    """The version of the aswap shim at ``path``; None when the file is not one, or is missing."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(512).decode("utf-8", "replace")
    except OSError:
        return None
    m = _MARKER_RE.search(head)
    return int(m.group(1)) if m else None


def is_shim(path) -> bool:
    return shim_version(path) is not None


def render(python: str, real: str) -> str:
    q = shlex.quote
    return (
        "#!/bin/sh\n"
        f"{MARKER} v{VERSION}. Remove it with: aswap shim uninstall\n"
        f"py={q(python)}\n"
        f"next={q(real)}\n"
        'if [ -x "$py" ]; then\n'
        '  export ASWAP_AGY="$next"\n'
        '  exec "$py" -I -m antigravity_swap.shim "$@"\n'
        "fi\n"
        'echo "aswap shim: $py not found; running agy without account switching" >&2\n'
        'exec "$next" "$@"\n'
    )


def _temp(d: Path, content: str) -> str:
    """An executable temp file with ``content`` in ``d``, ready to be renamed into place."""
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".agy.", suffix=".aswap-tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        os.chmod(tmp, 0o755)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return tmp


def _write(target: Path, content: str) -> None:
    os.replace(_temp(target.parent, content), target)


# --- the record --------------------------------------------------------------------------


def load_record() -> Record | None:
    raw = read_json(paths.shim_record())
    if not isinstance(raw, dict):
        return None
    try:
        return Record(shim=str(raw["shim"]), mode=str(raw["mode"]), real=str(raw["real"]),
                      python=str(raw["python"]), version=int(raw.get("version") or 0))
    except (KeyError, TypeError, ValueError):
        return None


def save_record(rec: Record) -> None:
    write_json(paths.shim_record(), {"schemaVersion": 1, **asdict(rec)})


# --- finding agy -------------------------------------------------------------------------


def _path_dirs(path_env: str | None = None) -> list[str]:
    return (os.environ.get("PATH", "") if path_env is None else path_env).split(os.pathsep)


def _find(path_env: str | None = None) -> tuple[Path, int] | None:
    """The first agy on PATH that is not an aswap shim, and the index of its PATH entry."""
    for i, d in enumerate(_path_dirs(path_env)):
        if not d:
            continue
        found = shutil.which("agy", path=d)
        if found and not is_shim(found):
            return Path(os.path.abspath(found)), i
    return None


def which_agy(path_env: str | None = None) -> str | None:
    """The first agy on PATH that is not an aswap shim."""
    hit = _find(path_env)
    return str(hit[0]) if hit else None


def recorded_real() -> str | None:
    """The agy behind the installed shim, while that shim is still in place.

    Once something else has replaced the shim (an agy update), the file in its place is the newer
    agy and the recorded one is stale, so nothing is returned.
    """
    rec = load_record()
    if rec is None or not is_shim(rec.shim) or not os.access(rec.real, os.X_OK):
        return None
    return rec.real


# --- install -----------------------------------------------------------------------------


def _same(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _writable(d: Path) -> bool:
    return d.is_dir() and os.access(d, os.W_OK | os.X_OK)


def _usable(d: str) -> bool:
    """Whether a PATH entry is a fit home for the shim: absolute, and not a directory another tool
    regenerates or that only exists in this shell (pyenv, rbenv, asdf or mise shims,
    node_modules/.bin, the active virtualenv or conda env)."""
    if not d or not os.path.isabs(d):
        return False
    p = Path(d)
    if "shims" in p.parts or "node_modules" in p.parts:
        return False
    for var in ("VIRTUAL_ENV", "CONDA_PREFIX"):
        env = os.environ.get(var)
        if env and _same(p, Path(env) / "bin"):
            return False
    return True


def install(out: TextIO) -> Record:
    """Put the shim where a plain ``agy`` finds it first. No fixed location:

    1. the directory of the first agy on PATH, if writable: the original becomes ``agy.real``;
    2. else the first fit, writable PATH directory ahead of it;
    3. else ``~/.local/bin``, with a note to put it on PATH ahead of the original.
    """
    if paths.IS_WINDOWS:
        raise ShimError("the shim is a sh script; Windows is not supported yet")
    if getattr(sys, "frozen", False):
        raise ShimError("the shim runs aswap's Python package, which the standalone binary does not "
                        "have; install aswap with uv or pipx to use it")
    if load_record() is not None:
        raise ShimError("a shim is already installed (see: aswap shim); remove it first: aswap shim uninstall")
    path_env = os.environ.get("PATH", "")
    hit = _find(path_env)
    if hit is None:
        raise ShimError("agy not found on PATH")
    try:
        with locked():
            rec = _place(hit[0], hit[1], sys.executable, path_env)
            save_record(rec)
    except OSError as e:
        raise ShimError(f"could not install the shim: {e}") from None
    if rec.mode == IN_PLACE:
        out.write(f"Installed the agy shim at {rec.shim}; the original agy is now {rec.real}.\n")
    else:
        out.write(f"Installed the agy shim at {rec.shim}; it starts {rec.real}.\n")
    first = shutil.which("agy", path=path_env)
    if first is None or not _same(Path(first), Path(rec.shim)):
        d = Path(rec.shim).parent
        out.write(f"{d} must come before {Path(rec.real).parent} on PATH for a plain `agy` to reach the shim. "
                  f"Add this to your shell's startup file:\n    export PATH=\"{d}:$PATH\"\n")
    out.write("The placement followed this shell's PATH; cron, systemd and IDEs may use a different one.\n")
    return rec


def _place(original: Path, index: int, python: str, path_env: str) -> Record:
    odir = original.parent
    if _writable(odir):
        real = odir / REAL_NAME
        if os.path.lexists(real):
            raise ShimError(f"{real} already exists; move it away, then install again")
        tmp = _temp(odir, render(python, str(real)))
        try:
            os.rename(original, real)
            try:
                os.replace(tmp, original)
            except OSError:
                os.rename(real, original)
                raise
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        return Record(str(original), IN_PLACE, str(real), python)
    for d in _path_dirs(path_env)[:index]:
        if _usable(d) and _writable(Path(d)):
            target = Path(d) / "agy"
            _write(target, render(python, str(original)))
            return Record(str(target), SEPARATE, str(original), python)
    fallback = Path.home() / ".local" / "bin"
    target = fallback / "agy"
    if os.path.lexists(target) and not is_shim(target):
        raise ShimError(f"{target} exists and is not aswap's shim; move it away, then install again")
    fallback.mkdir(parents=True, exist_ok=True)
    _write(target, render(python, str(original)))
    return Record(str(target), SEPARATE, str(original), python)


# --- uninstall ---------------------------------------------------------------------------


def uninstall(out: TextIO) -> None:
    """Remove the shim and, if it took the original's place, put the original back.

    A shim that something else has replaced is left alone, and so is ``agy.real``.
    """
    rec = load_record()
    if rec is None:
        raise ShimError("no shim is installed")
    shim = Path(rec.shim)
    real_there = os.path.lexists(rec.real)
    try:
        with locked():
            present, ours = os.path.lexists(shim), is_shim(shim)
            if present and not ours:
                out.write(f"{shim} is no longer aswap's shim (something replaced it); left it as it is"
                          + (f", and {rec.real} too" if rec.mode == IN_PLACE and real_there else "") + ".\n")
            elif rec.mode == IN_PLACE and real_there:
                os.replace(rec.real, shim)
                out.write(f"Removed the agy shim; {shim} is the original agy again.\n")
            elif present:
                shim.unlink()
                gone = f"; {rec.real} was already gone" if rec.mode == IN_PLACE else ""
                out.write(f"Removed the agy shim at {shim}{gone}.\n")
            else:
                out.write(f"The agy shim at {shim} was already gone.\n")
            paths.shim_record().unlink(missing_ok=True)
    except OSError as e:
        raise ShimError(f"could not remove the shim: {e}") from None


# --- keeping it working ------------------------------------------------------------------


def heal(err: TextIO) -> None:
    """Keep the installed shim working. Runs on every aswap command; cheap when nothing changed.

    - Something replaced the shim (an agy update wrote over it): when the shim took the original's
      place, the new file becomes ``agy.real``, replacing the old one (no backup), and the shim
      goes back.
    - The shim is older than this aswap, or the Python it names is gone: rewrite it.

    Must not be called while holding aswap's lock.
    """
    if load_record() is None:
        return
    try:
        with locked():
            rec = load_record()
            if rec is not None:
                _heal(rec, err)
    except OSError as e:
        err.write(f"aswap: could not restore the agy shim: {e}\n")


def _python(rec: Record) -> str:
    if os.access(rec.python, os.X_OK) or getattr(sys, "frozen", False):
        return rec.python
    return sys.executable


def _heal(rec: Record, err: TextIO) -> None:
    shim = Path(rec.shim)
    version = shim_version(shim)
    python = _python(rec)
    if version is None:
        if not os.path.lexists(shim):
            err.write(f"aswap: the agy shim at {shim} is gone; run: aswap shim uninstall\n")
            return
        if rec.mode != IN_PLACE:
            err.write(f"aswap: {shim} is no longer aswap's shim; to reinstall: aswap shim uninstall, "
                      f"then aswap shim install\n")
            return
        os.replace(shim, rec.real)
        _write(shim, render(python, rec.real))
        err.write(f"aswap: the agy shim at {shim} had been replaced; moved the new file to {rec.real} "
                  f"and put the shim back\n")
    elif version < VERSION or (version == VERSION and python != rec.python):
        _write(shim, render(python, rec.real))
    else:
        return
    rec.version, rec.python = VERSION, python
    save_record(rec)


# --- status ------------------------------------------------------------------------------


def status(out: TextIO) -> None:
    rec = load_record()
    if rec is None:
        out.write("No agy shim installed. Install it with: aswap shim install\n")
        return
    shim = Path(rec.shim)
    state = ("in place" if is_shim(shim) else
             "replaced by another file" if os.path.lexists(shim) else "missing")
    missing = " (missing)"
    out.write(f"Shim:      {shim} ({state})\n")
    out.write(f"Real agy:  {rec.real}{'' if os.access(rec.real, os.X_OK) else missing}\n")
    out.write(f"Python:    {rec.python}{'' if os.access(rec.python, os.X_OK) else missing}\n")
    out.write("Installed: " + ("in place of the original agy" if rec.mode == IN_PLACE
                               else "in its own directory on PATH") + "\n")
    first = shutil.which("agy")
    if first is None or not _same(Path(first), shim):
        out.write(f"A plain `agy` in this shell runs {first or 'nothing'}, not the shim; check PATH.\n")
