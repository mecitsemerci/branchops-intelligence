#!/usr/bin/env python3
"""Send the synthetic Jira webhook samples to a running BranchOps API, HMAC-signed.

Standard library only, so it runs without the project virtualenv:

    python3 scripts/send_samples.py [--url http://localhost:8080] [--secret dev-secret] [names...]
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SAMPLES = Path(__file__).resolve().parents[1] / "samples" / "jira"
# Order matters: creations first, then updates/comments; out-of-order and deletion samples are opt-in.
DEFAULT_ORDER = [
    "complete_incident",
    "duplicate_issue",
    "related_different_issue",
    "incomplete_incident",
    "generic_description",
    "missing_branch_code",
    "missing_application",
    "prompt_injection",
    "pii_incident",
    "cloud_adf_issue",
    "partial_update",
    "comment_created",
]


def send(url: str, secret: str, name: str) -> int:
    if not url.startswith(("http://", "https://")):
        raise SystemExit(f"unsupported URL scheme: {url}")
    body = (SAMPLES / f"{name}.json").read_bytes()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    request = urllib.request.Request(  # noqa: S310 - scheme validated above
        f"{url.rstrip('/')}/api/webhooks/jira",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "X-Hub-Signature": signature},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - scheme validated above
            result = json.loads(response.read())
            print(f"{name:28s} {response.status} {result.get('status')}")
            return 0
    except urllib.error.HTTPError as exc:
        print(f"{name:28s} {exc.code} {exc.read().decode(errors='replace')}", file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--url", default="http://localhost:8080")
    parser.add_argument("--secret", default="dev-secret")
    parser.add_argument("--delay", type=float, default=0.2, help="seconds between requests")
    parser.add_argument("names", nargs="*", help="sample names (default: a representative set)")
    args = parser.parse_args()
    failures = 0
    for name in args.names or DEFAULT_ORDER:
        failures += send(args.url, args.secret, name)
        time.sleep(args.delay)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
