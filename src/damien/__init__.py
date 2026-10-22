"""Damien — a Gmail triage CLI that asks before it acts."""

from importlib.metadata import PackageNotFoundError, version as _installed

try:
    # whatever was actually installed — one source of truth, so the version a user sees can
    # never disagree with the version they installed (it did, once: 0.1.0 against a v0.1.1 tag)
    __version__ = _installed("damien")
except PackageNotFoundError:                  # a checkout with no install around it
    __version__ = "0.0.0+source"
