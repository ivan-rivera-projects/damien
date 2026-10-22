"""Run the real CLI against a fake, offline Gmail — for recording the README demo.

    python scripts/demo.py <any damien args>

Nothing here touches a real mailbox: Gmail is the mock from the test suite, the judge is the
deterministic fake, and state lives in a throwaway DAMIEN_HOME that persists only for the
recording (DAMIEN_DEMO_HOME). The recording itself is `docs/assets/demo.tape` (vhs).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "src"))

home = Path(os.environ.get("DAMIEN_DEMO_HOME") or "/tmp/damien-demo")
home.mkdir(parents=True, exist_ok=True)
os.environ.update(DAMIEN_HOME=str(home), DAMIEN_JUDGE="fake", DAMIEN_ACCOUNT="you@example.com",
                  DAMIEN_READ_BODIES="off")

from conftest import FakeCreds, FakeGmailState  # noqa: E402
from damien import cli  # noqa: E402
from damien.gmail import Gmail  # noqa: E402

STATE = home / "fake-gmail.json"
fake = FakeGmailState()
fake.live_labels = True
if STATE.exists():
    saved = json.loads(STATE.read_text())
    fake.messages, fake.labels = saved["messages"], saved["labels"]
else:
    now = int(time.time() * 1000)
    day = 86_400_000
    rows = [
        ("Alex <alex@example.com>", "dinner friday?", 1),
        ("Your Accountant <cpa@ledger.example>", "Q1 numbers attached", 2),
        ("GovFiler <notice@govfiler.example>", "MOMDEM, LLC - 2026 Florida Annual Report", 3),
        ("Chase <alerts@chase.example>", "Your credit card statement is available", 4),
    ]
    rows += [(f"Deals <deals@shop{i}.example>", f"FINAL HOURS!!! {90 - i * 5}% OFF ENDS TONIGHT", 5 + i) for i in range(6)]
    rows += [(f"Digest <news@letters{i}.example>", f"Your weekly digest of digests #{i}", 3 + i) for i in range(5)]
    rows += [(f"Store <orders@store{i}.example>", f"Your order confirmation #{1000 + i}", 2 + i) for i in range(3)]
    rows += [(f"App <notifications@app{i}.example>", f"You have {i + 2} new notifications", 1 + i) for i in range(4)]
    for n, (sender, subject, age) in enumerate(rows):
        fake.add(f"m{n:02d}", subject=subject, sender=sender, date_ms=now - age * day,
                 list_unsub=sender.startswith("Digest"), snippet=subject)
    STATE.write_text(json.dumps({"messages": fake.messages, "labels": fake.labels}))


def _gmail(settings):
    g = Gmail(settings, http=httpx.Client(transport=httpx.MockTransport(fake.handler)))
    g._creds = FakeCreds()
    return g


cli._gmail = _gmail
try:
    cli.app(prog_name="damien")
finally:
    STATE.write_text(json.dumps({"messages": fake.messages, "labels": fake.labels}))
