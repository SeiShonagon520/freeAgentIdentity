"""Parse a captured ChatGPT registration HAR into a registration template.

The HAR is captured manually via camoufox (see the register dialog's
"camoufox 抓包模式").  This tool extracts the request sequence, the exact
headers sent (including origin/referer and sentinel tokens), cookies and the
interfaces the flow calls, so the protocol registration can replay the same
shape from a real-browser fingerprint.

Usage::

    python -m tools.har_to_template path/to/register-<task>.har [--out template.json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import OrderedDict
from urllib.parse import urlparse

# Endpoints that carry the actual registration state transitions.
REGISTRATION_HOSTS = ("chatgpt.com", "auth.openai.com", "api.openai.com", "sentinel.openai.com")
AUTH_PATH_MARKERS = (
    "api/auth", "signup", "signin", "register", "about-you", "otp", "verify",
    "email_otp", "sentinel", "csrf", "me", "token",
)


def _entries(har_path: str) -> list[dict]:
    with open(har_path, encoding="utf-8") as f:
        data = json.load(f)
    return list(data.get("log", {}).get("entries", []) or [])


def _is_registration_entry(entry: dict) -> bool:
    url = str(entry.get("request", {}).get("url") or "")
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        host = ""
    if not any(h in host for h in REGISTRATION_HOSTS):
        return False
    return any(marker in url for marker in AUTH_PATH_MARKERS)


def _header_map(headers: list) -> dict:
    return {str(h.get("name", "")).lower(): str(h.get("value", "")) for h in headers or []}


def extract_template(har_path: str) -> dict:
    entries = _entries(har_path)
    flow: list[dict] = []
    cookies: dict[str, str] = OrderedDict()
    for entry in entries:
        req = entry.get("request", {})
        resp = entry.get("response", {})
        url = str(req.get("url") or "")
        if not _is_registration_entry(entry):
            continue
        req_headers = _header_map(req.get("headers", []))
        resp_headers = _header_map(resp.get("headers", []))
        # Collect set-cookie names (values are dynamic; template keeps names).
        for raw in resp_headers.get("set-cookie", "").split(","):
            name = raw.split("=", 1)[0].strip()
            if name:
                cookies.setdefault(name, "")
        # Sentinel token / csrf are the interesting dynamic headers.
        sentinel = req_headers.get("openai-sentinel-token", "")
        csrf = req_headers.get("x-csrf-token", "") or req_headers.get("csrf-token", "")
        flow.append(
            {
                "method": str(req.get("method") or "GET").upper(),
                "url": url,
                "status": int(resp.get("status") or 0),
                "headers": {
                    k: v
                    for k, v in req_headers.items()
                    if k
                    in {
                        "accept",
                        "content-type",
                        "origin",
                        "referer",
                        "user-agent",
                        "openai-sentinel-token",
                        "x-csrf-token",
                        "oai-language",
                        "oai-device-id",
                        "chatgpt-account-id",
                    }
                },
                "has_sentinel": bool(sentinel),
                "has_csrf": bool(csrf),
                "post_body": str(req.get("postData", {}).get("text", "") or "")[:500],
            }
        )
    return {
        "har": os.path.basename(har_path),
        "total_entries": len(entries),
        "flow_entries": len(flow),
        "cookie_names": list(cookies.keys()),
        "flow": flow,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("har")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    template = extract_template(args.har)
    output = json.dumps(template, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"template written to {args.out}")
    else:
        print(output)


if __name__ == "__main__":
    main()
