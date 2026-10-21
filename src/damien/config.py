"""Damien v2 — configuration.

One place for paths, the Gmail account, and how judgment is made.
No service URLs, no ports: this is a library and a CLI, not a platform.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .stakes import DEFAULT_PROTECTED

# The only Gmail scope Damien needs: read, label, archive, trash, draft.
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.modify"


def default_root() -> Path:
    return Path(os.environ.get("DAMIEN_HOME") or (Path.home() / ".damien")).expanduser()


def load_env_file(path: Path) -> None:
    """Minimal KEY=VALUE loader. Real values are never printed; the file is gitignored."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _mask_url(value: str | None) -> str | None:
    """postgres://user:secret@host/db -> postgres://user:***@host/db. Never print a credential."""
    if not value:
        return value
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", value)


@dataclass(frozen=True)
class Settings:
    root: Path
    db_path: Path
    db_url: str | None           # postgres://… wins over db_path when set
    credentials_path: Path
    token_path: Path
    account: str
    scopes: tuple[str, ...]
    judge: str                    # 'rules' | 'openrouter' | 'typesafe' | 'fake'
    model: str
    api_key: str | None
    page_size: int                # Gmail list page size (<=500)
    op_batch: int                 # ids per Gmail mutate call (Gmail cap: 1000)
    judge_batch: int              # messages per judgment call
    confidence_act: float         # >= this: act unattended
    confidence_ask: float         # < this: a person should look
    protect_labels: tuple[str, ...]   # the person's own labels a model must not file away
    header_ttl_hours: float       # reuse stored headers younger than this (0 = always refetch)
    fetch_concurrency: int        # parallel Gmail metadata calls
    max_rps: float                # ceiling on Gmail requests/second (quota is per minute)

    def redacted(self) -> dict[str, object]:
        d = dict(self.__dict__)
        d.pop("api_key", None)
        if "db_url" in d:
            d["db_url"] = _mask_url(self.db_url)          # a connection string carries a password
        return {k: (str(v) if isinstance(v, Path) else v) for k, v in d.items()}


def load_settings(env_file: Path | None = None) -> Settings:
    root = default_root()
    root.mkdir(parents=True, exist_ok=True)
    load_env_file(env_file or root / ".env")
    load_env_file(Path.cwd() / ".env")

    def env(key: str, default: str = "") -> str:
        return os.environ.get(key, default).strip()

    def num(key: str, default: float) -> float:
        raw = env(key)
        try:
            return float(raw) if raw else default
        except ValueError:
            return default

    judge = env("DAMIEN_JUDGE", "rules").lower()
    # Pinned, not `jev-latest`: confidence thresholds are tuned against one version's behaviour.
    model = env("DAMIEN_MODEL", "typesafe/jev-1.13")
    key = env("TYPESAFE_API_KEY") or env("OPENROUTER_API_KEY") or None
    if judge in ("typesafe", "openrouter") and not key:
        judge = "rules"  # no key: degrade to rules only, never crash a run

    return Settings(
        root=root,
        db_path=Path(env("DAMIEN_DB") or (root / "damien.db")),
        db_url=env("DAMIEN_DB_URL") or None,
        credentials_path=Path(env("DAMIEN_CREDENTIALS") or (root / "credentials.json")),
        token_path=Path(env("DAMIEN_TOKEN") or (root / "token.json")),
        account=env("DAMIEN_ACCOUNT"),
        scopes=(GMAIL_SCOPE,),
        judge=judge,
        model=model,
        api_key=key,
        page_size=int(num("DAMIEN_PAGE_SIZE", 500)),
        op_batch=int(num("DAMIEN_OP_BATCH", 1000)),
        judge_batch=int(num("DAMIEN_JUDGE_BATCH", 8)),
        confidence_act=num("DAMIEN_CONFIDENCE_ACT", 0.80),
        confidence_ask=num("DAMIEN_CONFIDENCE_ASK", 0.55),
        header_ttl_hours=num("DAMIEN_HEADER_TTL_HOURS", 12.0),
        fetch_concurrency=int(num("DAMIEN_FETCH_CONCURRENCY", 8)),
        max_rps=num("DAMIEN_MAX_RPS", 8.0),
        protect_labels=tuple(
            p.strip() for p in (env("DAMIEN_PROTECT_LABELS") or ",".join(DEFAULT_PROTECTED)).split(",") if p.strip()
        ),
    )
