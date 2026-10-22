"""What is personal about an install lives here, not in the code.

The seven buckets are the tool's vocabulary. Their *descriptions*, and the line that frames
whose inbox this is, are the person's — and no two people answer them the same way. They
live in `~/.damien/profile.json`.

An install with no profile gets the neutral default, so a fresh clone is usable by a
stranger without editing any Python. An install with one gets its owner's framing, which is
what the model is told before it judges a single message.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from .decide import BUCKETS, DEFAULT_CONTEXT, Bucket, JudgementSpec

FILENAME = "profile.json"

TEMPLATE_README = (
    "Describe whose inbox this is, and what belongs in it. `context` is what the model is "
    "told about you before it judges anything — the single most valuable thing you can "
    "write here: what you do, and which subjects you actually want in the inbox. Each key "
    "under `buckets` replaces the default wording for that bucket (keep, receipt, "
    "newsletter, promotion, notification, archive, unclear); delete a key to keep the "
    "default. Leave `buckets` empty and the defaults apply to you."
)


class Profile(BaseModel):
    """The judgement a person makes about their own inbox."""

    context: str = DEFAULT_CONTEXT
    buckets: dict[str, str] = Field(default_factory=dict)   # bucket key -> its description

    def spec(self) -> JudgementSpec:
        """The spec this profile asks for: the tool's buckets, described their way."""
        known = {b.key: b for b in BUCKETS}
        unknown = sorted(set(self.buckets) - set(known))
        if unknown:
            raise ValueError(
                f"profile.json describes unknown bucket(s): {', '.join(unknown)} "
                f"(known: {', '.join(sorted(known))})"
            )
        buckets: list[Bucket] = [
            replace(b, description=self.buckets[b.key]) if b.key in self.buckets else b
            for b in BUCKETS
        ]
        return JudgementSpec(context=self.context, buckets=tuple(buckets))


def load_profile(root: Path | str) -> Profile:
    """The profile under `root`, or the neutral one when there is none."""
    path = Path(root) / FILENAME
    if not path.exists():
        return Profile()
    try:
        return Profile.model_validate(json.loads(path.read_text()))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"{path} is not a usable profile: {exc}") from exc


def write_profile(root: Path | str, *, force: bool = False) -> tuple[Path, bool]:
    """Write the template a new install starts from. Never overwrites without `force`.

    Returns the path and whether it was written, so a caller can say which happened.
    """
    path = Path(root) / FILENAME
    if path.exists() and not force:
        return path, False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"_readme": TEMPLATE_README, "context": DEFAULT_CONTEXT,
                                "buckets": {}}, indent=2, ensure_ascii=False) + "\n")
    return path, True
