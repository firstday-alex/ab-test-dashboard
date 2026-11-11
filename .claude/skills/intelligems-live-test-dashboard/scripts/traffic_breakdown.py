#!/usr/bin/env python3
"""
Break one landing-page group of the traffic sheet down by the landing page each
ad names, and print a `traffic.breakdowns` entry for the pulled JSON.

The sheet has no landing-page path column. Its "Other" bucket is ads whose
landing page matched no named group, so the only way to see where that traffic
goes is the ad name itself. Coded ad names carry the intended landing page as
`_URL-<page>`; that is the breakdown key. It's what the ad says it links to,
not a measured landing path, and the dashboard labels it that way.

Ads without a URL code fall back to their product code (`SKU-kde#...` -> kde).
Trybe creator ads (`...trybe=<id>`) carry neither code, so they pool together.

    python3 traffic_breakdown.py groups.csv --group Other
"""
import argparse
import csv
import json
import re
import sys
from collections import Counter

BASIS = "landing page named in the ad (_URL- code)"


def key_for(ad_name):
    name = (ad_name or "").strip()
    if not name:
        return "Blank ad name"
    m = re.search(r"_URL-([^_]+)", name)
    if m:
        # "kde-behind-the-science-lp - BM WL Ad 9/10/2026" is the same page.
        return m.group(1).split(" - ")[0].strip()
    if "trybe=" in name:
        return "Trybe creator ads (no URL code)"
    m = re.search(r"SKU-([a-z]+)", name)
    if m:
        return f"SKU {m.group(1)} (no URL code)"
    return "Uncoded ad name"


def breakdown(rows, group):
    """Parse by header name -- the sheet's column order has moved before."""
    out = Counter()
    for r in rows:
        if (r.get("Landing Page Path Groups") or "").strip() != group:
            continue
        raw = (r.get("Sessions") or "").replace(",", "").strip()
        if not raw:
            continue
        out[key_for(r.get("Ad Name"))] += float(raw)
    return [{"label": k, "sessions": v} for k, v in out.most_common()]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", help="Published traffic sheet CSV")
    ap.add_argument("--group", action="append", required=True,
                    help="Group to break down (repeatable)")
    args = ap.parse_args()
    with open(args.csv, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    print(json.dumps(
        {g: {"basis": BASIS, "rows": breakdown(rows, g)} for g in args.group},
        indent=2, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
