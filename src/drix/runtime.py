from __future__ import annotations

import os
import sys


def is_frozen() -> bool:
    """Return True when drix is running from a frozen standalone bundle."""

    return bool(getattr(sys, "frozen", False))


def external_process_environment(*, frozen: bool | None = None) -> dict[str, str]:
    """Return a safe environment for programs launched outside a frozen bundle."""

    env = dict(os.environ)
    if frozen is None:
        frozen = is_frozen()
    if not frozen:
        return env

    # PyInstaller prepends its private library directory so bundled extensions load
    # correctly. External system programs must see the user's original search path.
    for key in ("LD_LIBRARY_PATH", "LIBPATH", "DYLD_LIBRARY_PATH"):
        original_key = f"{key}_ORIG"
        if original_key in env:
            env[key] = env[original_key]
        else:
            env.pop(key, None)
    return env
