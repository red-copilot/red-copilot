"""Red Harness evaluation runtime."""

from __future__ import annotations

import os
from pathlib import Path

__version__ = "0.8.0"

_DOTENV_PATH = Path(".env")


def load_dotenv(path: str | Path = _DOTENV_PATH) -> None:
    """Populate ``os.environ`` from a local ``.env`` file.

    Values already present in the environment always win, so an explicit export
    overrides the file. Missing or unreadable files are ignored.
    """
    dotenv = Path(path)
    try:
        if not dotenv.is_file():
            return
        lines = dotenv.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip().strip('"').strip("'")


load_dotenv()
