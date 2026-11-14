"""Pure helpers for the "complete missing views" workflow in the Qt window.

The camera capture child process and the session directory are bound when the
launcher opens the source, long before the window exists (see
``tools/wizard/gui/app.py``).  Rebinding a running controller to a different
base session is unsafe, so the operator entry point in the window restarts the
*wizard process* with the correct flags instead of mutating the live one.

This module is deliberately free of Qt and of the controller: it only rewrites
a launcher argument vector, so the relaunch contract is testable offline.  The
window calls it right before ``QProcess.startDetached``.
"""
from __future__ import annotations

import os
from pathlib import Path

#: Options that carry a value and must never be inherited by the extension run.
#: Both ``--opt value`` and ``--opt=value`` are removed.
DROP_VALUE_OPTIONS = (
    "--session-dir",
    "--complete-session",
    "--duration",
    "--snapshot-out",
    "--extension-json",
    "--source",
    "--frames",
    "--recording",
    "--scene",
    "--views",
    "--noise",
    "--seed",
    "--max-groups",
)

#: Boolean switches that only make sense for the previous launch mode.
DROP_FLAGS = (
    "--resume",
    "--headless",
    "--autostart",
    "--demo",
    "--async-decisions",
    "--loop",
)

#: The extension run always captures from the scanner, never from a replay or a
#: synthetic scene.
FORCED_SOURCE = "live"

_LAUNCHER_NAME = "app.py"


def _looks_like_option(token: str) -> bool:
    """True when ``token`` is another option rather than a value for one.

    ``argparse`` would accept a negative number as ``--noise -0.5``; negative
    values are not meaningful for any dropped option, so a leading dash is
    treated as "this is the next option" and the token is kept for the main
    loop to classify.
    """
    text = str(token)
    return len(text) > 1 and text.startswith("-")


def _strip_launch_options(argv: list[str]) -> list[str]:
    """Remove the session/mode options that must not be inherited."""
    items = [str(a) for a in (argv or ())]
    kept: list[str] = []
    i = 0
    while i < len(items):
        token = items[i]
        name = token.split("=", 1)[0]
        if "=" in token and name in DROP_VALUE_OPTIONS:
            i += 1
            continue
        if token in DROP_FLAGS:
            i += 1
            continue
        if token in DROP_VALUE_OPTIONS:
            i += 1
            # ``--session-dir BASE``: drop the value too, unless the operator
            # forgot it and the next token is already a different option.
            if i < len(items) and not _looks_like_option(items[i]):
                i += 1
            continue
        kept.append(token)
        i += 1
    return kept


def build_extension_launch(argv: list[str], base_dir: str, new_dir: str,
                           app_path: str, python: str) -> list[str]:
    """Return the argv for a fresh wizard process that extends ``base_dir``.

    ``argv`` is the original launcher argument list *without* the program name
    (``sys.argv[1:]``).  Unrelated options (``--profile``, ``--mode``, ...) are
    preserved; every option that binds the old session, source or capture mode
    is dropped and replaced by ``--source live --complete-session <base>
    --session-dir <new>``.  The result starts with the interpreter and the
    launcher script, as required by ``QProcess.startDetached``.
    """
    args = _strip_launch_options(argv)
    args.extend(["--source", FORCED_SOURCE,
                 "--complete-session", str(base_dir),
                 "--session-dir", str(new_dir)])
    return [str(python), str(app_path), *args]


def derive_extension_dir(base: str) -> str:
    """Return the sibling directory used for the extension session.

    ``/sessions/cal-01`` -> ``/sessions/cal-01-ext``.  The base session is
    opened read-only and is never modified; the extension always gets its own
    directory next to it.
    """
    path = Path(os.path.expanduser(str(base)))
    if not path.name:
        # A filesystem root ("/") has no sibling name to extend.
        return str(path) + "-ext"
    return str(path.with_name(path.name + "-ext"))


def extension_target_problem(path: str) -> str:
    """Return why ``path`` cannot hold the extension session, or ``""``.

    ``begin_extension`` accepts a missing directory *or* an existing empty one
    (it writes its own manifest/config there); anything else is refused.  The
    expected refusal is reported as a plain reason string instead of an
    exception, because it is an operator-visible condition, not a bug.
    """
    target = Path(os.path.expanduser(str(path)))
    try:
        if target.is_file():
            return (f"{target} already exists as a file; the missing views "
                    f"need a new session directory")
        if not target.exists():
            return ""
        if any(target.iterdir()):
            return (f"{target} already exists and is not empty; the missing "
                    f"views need a new, empty session directory")
        return ""
    except OSError as exc:
        return f"cannot inspect {target}: {type(exc).__name__}: {exc}"


def launcher_path(argv0: str | None = None) -> str:
    """Return the path of the wizard launcher script to restart.

    ``argv0`` is used when it really names ``app.py`` (the normal CLI case);
    otherwise the launcher that sits next to this module is used, so a
    programmatic ``main(argv)`` call (tests, tooling) still restarts the right
    file instead of the caller's script.
    """
    if argv0:
        candidate = Path(str(argv0))
        try:
            if candidate.name == _LAUNCHER_NAME and candidate.is_file():
                return str(candidate.resolve())
        except OSError:
            pass
    return str(Path(__file__).resolve().with_name(_LAUNCHER_NAME))
