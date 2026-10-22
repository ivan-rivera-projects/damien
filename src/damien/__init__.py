"""Damien — a Gmail triage CLI that asks before it acts."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed

try:
    # Whatever was actually installed. It is the honest answer for a wheel install, but an
    # *editable* install runs this source tree while carrying the metadata of whenever it was
    # installed — so `source_version()` below exists to notice when the two disagree.
    __version__ = _installed("damien")
except PackageNotFoundError:                  # a checkout with no install around it
    __version__ = "0.0.0+source"


def source_version() -> str | None:
    """The version this *source tree* declares, or None when there is no tree (a wheel).

    `pyproject.toml` is the single source of truth for the version; the installed metadata is a
    copy of it made at install time. A floor check that reads only the copy can stop a machine
    that is in fact running newer code.
    """
    import tomllib
    from pathlib import Path

    for parent in Path(__file__).resolve().parents:
        candidate = parent / "pyproject.toml"
        if candidate.is_file():
            try:
                return tomllib.loads(candidate.read_text()).get("project", {}).get("version")
            except (OSError, tomllib.TOMLDecodeError):
                return None
    return None


def version_note() -> str | None:
    """A line for `damien version` when the metadata is stale, else None."""
    declared = source_version()
    if declared and declared != __version__:
        return (f"  running from a source tree that declares {declared} — the installed "
                f"metadata says {__version__} (refresh it: uv tool install --editable "
                f"'.[pg,vectors]' --force)")
    return None
