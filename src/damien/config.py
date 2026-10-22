"""Damien v2 — configuration.

One place for paths, the Gmail account, and how judgment is made.
No service URLs, no ports: this is a library and a CLI, not a platform.
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .stakes import DEFAULT_PROTECTED

# The only Gmail scope Damien needs: read, label, archive, trash, draft.
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.modify"


def default_root() -> Path:
    return Path(os.environ.get("DAMIEN_HOME") or (Path.home() / ".damien")).expanduser()


ENV_TEMPLATE = """# Damien — your install's settings. Never commit real values.

# The account this install works on (used to label stored rows).
DAMIEN_ACCOUNT=

# rules      = deterministic only, no model calls, no key, nothing sent anywhere
# openrouter = a decision model through OpenRouter's decisions endpoint (needs OPENROUTER_API_KEY)
# typesafe   = Jev direct from TypeSafe (needs TYPESAFE_API_KEY)
# local      = a decision model you host yourself (needs DAMIEN_JUDGE_URL; DAMIEN_JUDGE_KEY if it asks for one)
# fake       = deterministic stand-in, for a demo with no key
DAMIEN_JUDGE=rules
# Any System-One decision model: state + typed questions in, choice/confidence/probabilities out.
# On openrouter, e.g. typesafe/jev-1.14 or perplexity/pplx-decider-flash-v1.2. A model is only
# trusted to act unattended once `damien judge eval` has calibrated it (jev-1.13 keeps the
# 0.9 bar it was tuned at).
DAMIEN_MODEL=typesafe/jev-1.13
OPENROUTER_API_KEY=
# TYPESAFE_API_KEY=
# DAMIEN_JUDGE_URL=http://127.0.0.1:8080/v1/systemone   (overrides the backend's default URL)
# DAMIEN_JUDGE_KEY=
# Parallel judgment requests (the public endpoints cap near eight).
# DAMIEN_JUDGE_CONCURRENCY=8

# Unset = one SQLite file at ~/.damien/damien.db. Set = Postgres.
# DAMIEN_DB_URL=postgres://user:password@127.0.0.1:5432/damien

# off | on_escalation. on_escalation reads the newest text of the messages the headers and
# snippet could not settle, and asks again - the only mode where message text reaches the
# judge (see docs/PRIVACY.md). What it reads is kept locally; attachments are never opened.
DAMIEN_READ_BODIES=off

# Labels the model may never act against: anything carrying one is kept.
DAMIEN_PROTECT_LABELS=ACTION REQUIRED,WAITING FOR,Bills

# 1 = filing a message (archiving it, labelling it out of the inbox) also marks it read, so
# "unread" keeps meaning "you have not looked at this yet". Kept mail is never touched.
# DAMIEN_MARK_READ_WHEN_FILED=0
"""


def write_env(root: Path | str, *, force: bool = False) -> tuple[Path, bool]:
    """Write the settings template, 0600 (it will hold a key). Never clobbers without force.

    The template lives here rather than in a file beside the checkout, because an install
    from a wheel has no checkout — that bug made `init` silently skip the .env.
    """
    path = Path(root) / ".env"
    if path.exists() and not force:
        return path, False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ENV_TEMPLATE)
    path.chmod(0o600)
    return path, True


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


# Each backend reads its own key, so a TypeSafe key is never sent to OpenRouter. A local
# judge's key is optional (most self-hosted servers take none).
KEY_FOR = {"typesafe": "TYPESAFE_API_KEY", "openrouter": "OPENROUTER_API_KEY", "local": "DAMIEN_JUDGE_KEY"}
KEY_REQUIRED = ("typesafe", "openrouter")
DEFAULT_URL = {"typesafe": "https://api.typesafe.ai/v1/systemone",
               "openrouter": "https://openrouter.ai/api/alpha/decisions"}


@dataclass(frozen=True)
class Settings:
    root: Path
    db_path: Path
    db_url: str | None           # postgres://… wins over db_path when set
    credentials_path: Path
    token_path: Path
    account: str
    scopes: tuple[str, ...]
    judge: str                    # 'rules' | 'openrouter' | 'typesafe' | 'local' | 'fake' — as requested
    model: str
    api_key: str | None           # the key for *this* backend only (KEY_FOR), never another's
    page_size: int                # Gmail list page size (<=500)
    op_batch: int                 # ids per Gmail mutate call (Gmail cap: 1000)
    judge_batch: int              # messages per judgment call
    confidence_act: float         # >= this: act unattended
    confidence_ask: float         # < this: a person should look
    protect_labels: tuple[str, ...]  # the person's own labels a model must not file away
    header_ttl_hours: float       # reuse stored headers younger than this (0 = always refetch)
    fetch_concurrency: int        # parallel Gmail metadata calls
    max_rps: float                # ceiling on Gmail requests/second (quota is per minute)
    mark_read_when_filed: bool = False  # filing also drops UNREAD, so unread means "not looked at"
    read_bodies: str = "off"            # off | on_escalation — read the text only where the snippet failed
    judge_url: str | None = None        # DAMIEN_JUDGE_URL: overrides the backend default; required for local
    judge_concurrency: int = 8          # parallel judgment requests

    def redacted(self) -> dict[str, object]:
        d = dict(self.__dict__)
        d.pop("api_key", None)
        if "db_url" in d:
            d["db_url"] = _mask_url(self.db_url)          # a connection string carries a password
        if d.get("judge_url"):
            d["judge_url"] = _mask_url(self.judge_url)
        return {k: (str(v) if isinstance(v, Path) else v) for k, v in d.items()}

    def judge_endpoint(self) -> str | None:
        """Where judgment requests go: DAMIEN_JUDGE_URL, else the backend's default."""
        return self.judge_url or DEFAULT_URL.get(self.judge)

    def judge_host(self) -> str | None:
        url = self.judge_endpoint()
        return (urlparse(url).hostname or None) if url else None

    def judge_is_local(self) -> bool:
        """True when the judge runs on this machine or this network: nothing leaves for a third party."""
        host = self.judge_host()
        if not host:
            return False
        if host == "localhost" or host.endswith((".local", ".localhost")):
            return True
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return False
        return ip.is_loopback or ip.is_private or ip.is_link_local


def load_settings(env_file: Path | None = None) -> Settings:
    root = default_root()
    root.mkdir(parents=True, exist_ok=True)
    # Only the install's own .env: one in whatever directory the command runs from could
    # silently swap the judge or its key (S3).
    load_env_file(env_file or root / ".env")

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
    # The requested judge is kept even without its key: make_judge then returns None (rules
    # only, never a crash) and doctor can say what is missing instead of hiding it.
    key = (env(KEY_FOR[judge]) or None) if judge in KEY_FOR else None

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
        mark_read_when_filed=env("DAMIEN_MARK_READ_WHEN_FILED").lower() in ("1", "true", "yes", "on"),
        read_bodies=("on_escalation"
                     if env("DAMIEN_READ_BODIES").strip().lower() in ("on_escalation", "1", "true", "yes", "on")
                     else "off"),
        judge_url=env("DAMIEN_JUDGE_URL") or None,
        judge_concurrency=max(1, int(num("DAMIEN_JUDGE_CONCURRENCY", 8))),
    )
