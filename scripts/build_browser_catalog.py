#!/usr/bin/env python3
"""Build a curated Chromium website catalog from the official Tranco list."""

from __future__ import annotations

import argparse
import csv
import io
import json
from pathlib import Path
import re
import urllib.request
import zipfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "assets" / "browser" / "tranco_safe_top1000.csv"
DEFAULT_SUMMARY = PROJECT_ROOT / "assets" / "browser" / "tranco_safe_top1000.summary.json"
SOURCE_URL = "https://tranco-list.eu/top-1m.csv.zip"

EXACT_DENYLIST = {
    "accounts.google.com",
    "akadns.net",
    "akamai.net",
    "akamaiedge.net",
    "amazonaws.com",
    "apple-dns.net",
    "app-measurement.com",
    "appsflyersdk.com",
    "domaincontrol.com",
    "doubleclick.net",
    "e2ro.com",
    "goo.gl",
    "googlesyndication.com",
    "googletagmanager.com",
    "googlevideo.com",
    "googleapis.com",
    "googleusercontent.com",
    "gtld-servers.net",
    "icloud.com",
    "login.live.com",
    "microsoftonline.com",
    "office.com",
    "office.net",
    "ntp.org",
    "outlook.live.com",
    "mail.google.com",
    "teams.microsoft.com",
    "slack.com",
    "sharepoint.com",
    "zoom.us",
    "trafficmanager.net",
    "whatsapp.net",
    "windows.net",
    "workers.dev",
    "windowsupdate.com",
    "web.whatsapp.com",
    "discord.com",
    "chatgpt.com",
    "openai.com",
}

KEYWORD_DENYLIST = (
    "adult",
    "adsystem",
    "adtraffic",
    "analytics",
    "adnxs",
    "adsrvr",
    "bet",
    "bytefcdn",
    "bookmaker",
    "captive",
    "casino",
    "cdn",
    "crypto",
    "dns",
    "doubleclick",
    "criteo",
    "demdex",
    "edgekey",
    "edgesuite",
    "escort",
    "fastly",
    "gambl",
    "gstatic",
    "hentai",
    "ipify",
    "klaviyo",
    "login",
    "measure",
    "nsfw",
    "moatads",
    "onlyfans",
    "omtrdc",
    "openx",
    "porn",
    "portal",
    "pubmatic",
    "quantserve",
    "rubicon",
    "scorecardresearch",
    "sex",
    "smartadserver",
    "taboola",
    "telemetry",
    "traffic",
    "torrent",
    "track",
    "tracking",
    "wallet",
    "webcam",
    "worker",
    "yieldmo",
    "xxx",
)

SUBDOMAIN_PREFIX_DENYLIST = {
    "accounts",
    "ads",
    "adservice",
    "analytics",
    "api",
    "auth",
    "cdn",
    "click",
    "img",
    "images",
    "login",
    "mail",
    "m",
    "static",
    "track",
    "tracking",
}


def _homepage_url(domain: str) -> str:
    return f"https://{domain}/"


def _looks_safe_public_site(domain: str) -> bool:
    domain = domain.strip().lower()
    if not domain or "." not in domain:
        return False
    if domain in EXACT_DENYLIST:
        return False
    if any(keyword in domain for keyword in KEYWORD_DENYLIST):
        return False
    labels = domain.split(".")
    if len(labels) >= 3 and labels[0] in SUBDOMAIN_PREFIX_DENYLIST:
        return False
    if re.search(r"(cdn|static|img|images|media|assets)\d*\.", domain):
        return False
    if re.search(r"(cloudfront|amazonaws|googlevideo|googleapis|googletagmanager|googlesyndication|akadns|akamai|edgekey|edgesuite|doubleclick|gstatic|gvt\d*|2mdn)\.", domain):
        return False
    return True


def build_catalog(*, source_limit: int, count: int) -> tuple[list[dict[str, str]], dict[str, int]]:
    with urllib.request.urlopen(SOURCE_URL, timeout=60) as response:
        payload = response.read()

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        member = archive.namelist()[0]
        with archive.open(member) as handle:
            text = io.TextIOWrapper(handle, encoding="utf-8", newline="")
            reader = csv.reader(text)
            rows: list[dict[str, str]] = []
            seen: set[str] = set()
            inspected = 0
            skipped = 0
            for rank_raw, domain_raw in reader:
                inspected += 1
                if inspected > source_limit:
                    break
                domain = domain_raw.strip().lower()
                if domain in seen or not _looks_safe_public_site(domain):
                    skipped += 1
                    continue
                seen.add(domain)
                rows.append(
                    {
                        "rank": str(int(rank_raw)),
                        "domain": domain,
                        "url": _homepage_url(domain),
                    }
                )
                if len(rows) >= count:
                    break

    summary = {
        "source_limit": source_limit,
        "requested_count": count,
        "accepted_count": len(rows),
        "inspected_count": inspected,
        "skipped_count": skipped,
    }
    return rows, summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--source-limit", type=int, default=10_000)
    parser.add_argument("--count", type=int, default=1_000)
    args = parser.parse_args()

    rows, summary = build_catalog(source_limit=args.source_limit, count=args.count)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["rank", "domain", "url"])
        writer.writeheader()
        writer.writerows(rows)

    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
