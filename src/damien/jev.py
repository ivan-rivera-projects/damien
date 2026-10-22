"""The TypeSafe / Jev layer — a System One model, used the way its docs say to use it.

Design decisions here are all traceable to the vendor documentation:

  * **One message per request, every question about it in that request.**
    Questions in a call are independent and parallel (adding them "barely changes
    the response time"); sharing one `state` across many emails instead costs
    accuracy ("Jev suffers from context rot… unrelated material in the state
    costs you accuracy"). So the unit of work is one email; the pool is what
    gives throughput.
  * **Atomic questions only** — one bucket Choice, one Noul per yes/no, one Score
    per ordered dimension. Never "analyze this and decide".
  * **A companion Noul guards the Choice**, because a Choice always picks one
    option even when none fit.
  * **Raw probabilities are kept**, not just the chosen bucket, so thresholds can
    be re-tuned later without paying for the requests again.
  * **Confidence, not probabilities, gates action**: 0–1, where 1 means the mass
    sits on one outcome. It describes the answer, not its correctness — which is
    exactly why the bands are conservative and a person is the last resort.
  * **Never ask it to count, order or compute** — that is code's job.
  * Model ids are pinned when thresholds matter (`typesafe/jev-1.13`).
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import DEFAULT_URL, KEY_REQUIRED, Settings
from .decide import JudgementSpec, Verdict
from .models import Message
from .stakes import IMPORTANCE_LEVELS

TYPESAFE_URL = DEFAULT_URL["typesafe"]
OPENROUTER_SYSTEMONE_URL = "https://openrouter.ai/api/v1/systemone"
OPENROUTER_DECISIONS_URL = DEFAULT_URL["openrouter"]

def message_state(message: Message, context: str = "", label_names: dict[str, str] | None = None) -> dict[str, Any]:
    """Small on purpose: headers + snippet. Jev is weakened by irrelevant material.

    Two things are added because they are free and worth more than anything the
    model could infer: `gmail_category` (Google already classified it) and
    `your_labels` (the person already decided).
    """
    names = label_names or {}
    block: dict[str, Any] = {
        "from": f"{message.from_name or ''} <{message.from_addr or ''}>".strip(),
        "subject": message.subject or "",
        "snippet": (message.snippet or "")[:600],
        "has_attachment": message.has_attachment,
        "is_mailing_list": bool(message.list_id or message.list_unsubscribe),
    }
    if message.body:
        # The newest text only, and only for messages the snippet could not settle. It is
        # part of the state, so it is part of the cache key: an escalated judgement and a
        # snippet-only judgement of the same message are different answers, cached apart.
        block["body"] = message.body
    if message.category:
        block["gmail_category"] = message.category
    own = [names.get(lid, lid) for lid in message.own_label_ids]
    if own:
        block["your_labels"] = own
    state: dict[str, Any] = {"message": block}
    if context:
        state["reader"] = context
    return state


def build_questions(spec: JudgementSpec) -> dict[str, dict[str, Any]]:
    """Every question about one message, asked once, in one call."""
    return {
        "bucket": {
            "type": "choice",
            "instructions": "Which of these best describes `message` for `reader`? "
                            "`message.gmail_category` is Google's own tab guess about it; "
                            "`message.your_labels`, where present, are labels `reader` applied "
                            "themselves — those matter more.",
            "criteria": {b.key: b.description for b in spec.buckets},
        },
        "handled_by_a_machine": {
            "type": "noul",
            "instructions": "Was `message` sent by software rather than by a person?",
        },
        "wants_a_reply": {
            "type": "noul",
            "instructions": "Does `message` ask `reader` something or expect a reply from them?",
        },
        "about_their_work": {
            "type": "noul",
            "instructions": "Is `message` about `reader`'s own work, clients, bookings, money or projects?",
        },
        "importance": {
            "type": "score",
            "instructions": "How much does `message` matter to `reader`?",
            "criteria": IMPORTANCE_LEVELS,
        },
    }


def parse_answer(message_id: str, answers: dict[str, Any]) -> Verdict:
    bucket_answer = answers.get("bucket") or {}
    choice = str(bucket_answer.get("choice") or "unclear")
    confidence = bucket_answer.get("confidence")
    probabilities = bucket_answer.get("probabilities")

    notes: list[str] = []
    nouls = {k: answers.get(k, {}).get("noul") for k in ("handled_by_a_machine", "wants_a_reply", "about_their_work")}
    if nouls["handled_by_a_machine"] is not None:
        notes.append(f"machine-sent p={nouls['handled_by_a_machine']:.2f}")
    if nouls["wants_a_reply"] is not None:
        notes.append(f"wants-a-reply p={nouls['wants_a_reply']:.2f}")
    if nouls["about_their_work"] is not None:
        notes.append(f"about-their-work p={nouls['about_their_work']:.2f}")
    score = answers.get("importance", {}).get("score")
    if score is not None:
        notes.append(f"importance={score:.2f}")

    return Verdict(
        message_id=message_id, choice=choice, confidence=confidence, probabilities=probabilities,
        signals={**nouls, "importance": score}, rationale="; ".join(notes) or None,
    )


class JevJudge:
    """A judge backed by a System One model. One request per message, pooled."""

    def __init__(self, settings: Settings, spec: JudgementSpec, *, concurrency: int | None = None,
                 transport: httpx.BaseTransport | None = None, url: str | None = None,
                 backend: str | None = None, cache: Any = None):
        self.settings = settings
        self.spec = spec
        # DAMIEN_JUDGE_CONCURRENCY: the public endpoints cap near eight, a local server may take more.
        self.concurrency = max(1, concurrency or settings.judge_concurrency)
        self.name = backend or settings.judge
        self.url = (url or settings.judge_url
                    or (TYPESAFE_URL if self.name == "typesafe" else OPENROUTER_DECISIONS_URL))
        self.failed: set[str] = set()          # ids whose judgment raised this run — never cached
        self._client = httpx.Client(timeout=60.0, transport=transport)
        self.calls = 0
        self.cached = 0
        self.spent_usd = 0.0
        self.cache = cache                     # anything with get(key) / put(key, id, model, verdict)

    # ---- transport ----------------------------------------------------
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.settings.api_key:                  # a local judge may take no key at all
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        if self.url == TYPESAFE_URL:
            headers["TypeSafe-Danger-Zone-Long-Context"] = "true"
        return headers

    def _body(self, state: dict, questions: dict) -> dict:
        body: dict[str, Any] = {"model": self.settings.model, "state": state, "questions": questions}
        if self.url != TYPESAFE_URL:
            body["session_id"] = "damien"
        return body

    def _post(self, message: Message, context: str) -> Verdict:
        body = self._body(message_state(message, context, getattr(self, "label_names", None)), build_questions(self.spec))
        delay = 0.4
        last: Exception | None = None
        for _attempt in range(5):
            try:
                resp = self._client.post(self.url, headers=self._headers(), json=body)
            except httpx.HTTPError as exc:
                last = exc
            else:
                if resp.status_code == 200:
                    data = resp.json()
                    self.calls += 1
                    cost = (data.get("usage") or {}).get("cost")
                    if isinstance(cost, (int, float)):
                        self.spent_usd += float(cost)
                    answers = data.get("answers") or {}
                    # A chat model behind the same URL answers 200 with no confidence. Treating
                    # that as "unsure" would cache it forever; it is a failure (J6).
                    if (answers.get("bucket") or {}).get("confidence") is None:
                        raise RuntimeError("backend returned no confidence — not a decision model")
                    return parse_answer(message.id, answers)
                if resp.status_code in (429, 500, 502, 503, 504, 529):
                    last = RuntimeError(f"{resp.status_code}: {resp.text[:200]}")
                elif resp.status_code in (401, 402, 403):
                    raise RuntimeError(f"Jev refused the call ({resp.status_code}): {resp.text[:200]}")
                else:
                    raise RuntimeError(f"Jev error {resp.status_code}: {resp.text[:300]}")
            time.sleep(delay + random.random() * 0.2)
            delay = min(delay * 2, 8.0)
        raise RuntimeError(f"gave up calling Jev for {message.id}: {last}")

    def verdict_key(self, message: Message, context: str, label_names: dict[str, str] | None) -> str:
        """The cache key is the request: if the state and the questions are identical,
        the answer is identical, so it never needs buying twice. Changing the model,
        a question's wording, or the person's own labels on the message all invalidate it."""
        payload = "|".join([
            self.settings.model,
            urlparse(self.url).hostname or "",     # the same model id on another host is another model
            json.dumps(build_questions(self.spec), sort_keys=True, default=str),
            json.dumps(message_state(message, context, label_names), sort_keys=True, default=str),
        ])
        return hashlib.sha256(payload.encode()).hexdigest()

    # ---- the Judge interface -----------------------------------------
    def judge(self, messages: Sequence[Message], buckets: dict[str, str], *, context: str = "",
              label_names: dict[str, str] | None = None) -> list[Verdict]:
        self.label_names = label_names or {}
        # `failed` accumulates for the whole pass: decide_all calls this once per chunk, and
        # build_plan reads it afterwards to keep failed messages out of escalation.
        keys = {m.id: self.verdict_key(m, context, label_names) for m in messages}
        by_id: dict[str, Verdict] = {}
        todo: list[Message] = []

        for message in messages:
            hit = self.cache.get(keys[message.id]) if self.cache else None
            if hit:
                # The key is the *state*, and two messages can share one exactly (same
                # sender, subject, snippet, labels). The cached verdict therefore carries
                # whichever message_id was stored first: answer for this one, not that one.
                self.cached += 1
                by_id[message.id] = Verdict(**{**hit, "message_id": message.id})
            else:
                todo.append(message)

        if todo:
            with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
                futures = [pool.submit(self._post, m, context) for m in todo]
                for message, future in zip(todo, futures, strict=True):
                    try:
                        verdict = future.result()
                    except Exception as exc:              # one bad message never kills a plan
                        verdict = Verdict(message_id=message.id, choice="unclear", confidence=0.0,
                                          rationale=f"judgment failed: {str(exc)[:120]}")
                        self.failed.add(message.id)
                        by_id[message.id] = verdict
                        # A failure is about this call (402, outage), not the message: caching
                        # it would keep the message unjudged after the key is topped up (L1).
                        continue
                    self.failed.discard(message.id)
                    by_id[message.id] = verdict
                    if self.cache:
                        self.cache.put(keys[message.id], message.id, self.settings.model,
                                       verdict.model_dump(mode="json"))
        return [by_id[m.id] for m in messages]            # original order, cache or not


class FakeJudge:
    """Deterministic stand-in for tests and dry runs. No network."""

    name = "fake"

    def __init__(self, mapping: Callable[[Message], tuple[str, float]] | None = None):
        self.mapping = mapping or _heuristic
        self.calls = 0
        self.cached = 0
        self.spent_usd = 0.0
        self.failed: set[str] = set()

    def judge(self, messages: Sequence[Message], buckets: dict[str, str], *, context: str = "",
              label_names: dict[str, str] | None = None) -> list[Verdict]:
        out = []
        for message in messages:
            self.calls += 1
            choice, confidence = self.mapping(message)
            if choice not in buckets:
                choice = "unclear"
            out.append(Verdict(message_id=message.id, choice=choice, confidence=confidence,
                               probabilities={choice: confidence}, rationale="fake judge"))
        return out


def _heuristic(message: Message) -> tuple[str, float]:
    subject = (message.subject or "").lower()
    sender = (message.from_addr or "").lower()
    if message.list_unsubscribe:
        return "newsletter", 0.9
    if "receipt" in subject or "order" in subject or "confirmation" in subject:
        return "receipt", 0.9
    if "sale" in subject or "% off" in subject or "deal" in subject:
        return "promotion", 0.85
    if sender.startswith(("noreply", "no-reply", "notifications")):
        return "notification", 0.8
    return "keep", 0.6


def make_judge(settings: Settings, spec: JudgementSpec, cache: Any = None) -> JevJudge | FakeJudge | None:
    """Pick a judge from settings. `rules` means no judge at all — rules only.

    A backend missing what it needs (a key, or a URL for `local`) is also no judge:
    the run degrades to rules and keeps the rest, and doctor names the missing piece.
    """
    if settings.judge == "fake":
        return FakeJudge()
    if settings.judge in KEY_REQUIRED and settings.api_key:
        return JevJudge(settings, spec, cache=cache)
    if settings.judge == "local" and settings.judge_url:
        return JevJudge(settings, spec, cache=cache)
    return None
