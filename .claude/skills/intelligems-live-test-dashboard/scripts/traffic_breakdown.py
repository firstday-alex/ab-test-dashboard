#!/usr/bin/env python3
"""
Break traffic-sheet groups down by actual landing page, and move a live test's
redirect pages into the group the test runs on.

The published sheet has two tabs:
  groups    Ad Name, Landing Page Path Groups, Sessions, ...
  full_lps  Ad Name, Landing Page, Sessions, ...

Neither tab says which landing page belongs to which group, so that mapping is
read off the data: an ad that has exactly one row in each tab ties its landing
page to its group. A page with no such ad stays unmapped and is not itemised.
The tabs are cut off at 1,000 rows each, so their totals differ by about 1%.
Each breakdown therefore ends with a "not itemised" line, so the sub-rows
still add up to the groups-tab total the dashboard shows.

--reassign PATH=GROUP moves a landing page's sessions into GROUP. Use it only
for a live test's redirect destination, read from that test's Intelligems
config. The sheet records the page after the redirect, so without this the
challenger arm's sessions leave the test's group and show up in "Other".
Example: [KDE-BTS] vs Single Page PLP FBB sends half of
/pages/kde-behind-the-science-lp to /pages/family-bundle-builder-gridwall-builder,
which the sheet files under Other.

    python3 traffic_breakdown.py groups.csv full_lps.csv --group Other \\
        --reassign /pages/family-bundle-builder-gridwall-builder=KDE-BTS

Prints a JSON fragment with `groups`, `breakdowns` and `reassigned`, to merge
into the pulled JSON's `traffic` block.
"""
import argparse
import csv
import json
import sys
from collections import Counter, defaultdict

GROUP_COL = "Landing Page Path Groups"
LP_COL = "Landing Page"
BASIS = "actual landing page (sheet's full_lps tab)"


def sessions(row):
    raw = (row.get("Sessions") or "").replace(",", "").strip()
    return float(raw) if raw else 0.0


def read(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))     # by header; column order has moved before


def lp_groups(group_rows, lp_rows):
    """Landing page -> group, from ads that map one-to-one across the tabs."""
    by_ad_g, by_ad_l = defaultdict(set), defaultdict(set)
    for r in group_rows:
        by_ad_g[r["Ad Name"]].add((r.get(GROUP_COL) or "").strip())
    for r in lp_rows:
        by_ad_l[r["Ad Name"]].add((r.get(LP_COL) or "").strip())
    votes = defaultdict(Counter)
    for ad, lps in by_ad_l.items():
        gs = by_ad_g.get(ad, set())
        if len(lps) == 1 and len(gs) == 1:
            votes[next(iter(lps))][next(iter(gs))] += 1
    out, conflicts = {}, []
    for lp, v in votes.items():
        if len(v) > 1:
            conflicts.append((lp, dict(v)))
        else:
            out[lp] = next(iter(v))
    return out, conflicts


def build(group_rows, lp_rows, groups_to_break, reassign):
    totals = Counter()
    for r in group_rows:
        totals[(r.get(GROUP_COL) or "").strip()] += sessions(r)
    lp_sess = Counter()
    for r in lp_rows:
        lp_sess[(r.get(LP_COL) or "").strip()] += sessions(r)
    mapping, conflicts = lp_groups(group_rows, lp_rows)

    moved = []
    for path, target in reassign:
        src = mapping.get(path)
        n = lp_sess.get(path, 0.0)
        if src is None or n == 0:
            raise SystemExit(f"--reassign {path}: page not found in the sheet or not mapped to a group")
        if src == target:
            continue                    # the sheet already counts it there
        totals[src] -= n
        totals[target] += n
        mapping[path] = target
        moved.append({"path": path, "from": src, "to": target, "sessions": n})

    breakdowns = {}
    for g in groups_to_break:
        items = sorted(((lp, lp_sess[lp]) for lp, grp in mapping.items() if grp == g),
                       key=lambda t: -t[1])
        rows = [{"label": lp, "sessions": s} for lp, s in items if s]
        rest = totals[g] - sum(s for _, s in items)
        if rest >= 0.5:
            rows.append({"label": "Not itemised (the two sheet tabs disagree slightly)",
                         "sessions": rest, "rest": True})
        breakdowns[g] = {"basis": BASIS, "rows": rows}

    if conflicts:
        print(f"warning: pages mapped to more than one group, not itemised: {conflicts}",
              file=sys.stderr)
    return {"groups": {k: v for k, v in totals.items() if v}, "breakdowns": breakdowns,
            "reassigned": moved}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("groups_csv")
    ap.add_argument("lps_csv")
    ap.add_argument("--group", action="append", required=True)
    ap.add_argument("--reassign", action="append", default=[], metavar="PATH=GROUP")
    args = ap.parse_args()
    reassign = []
    for spec in args.reassign:
        path, _, grp = spec.partition("=")
        if not grp:
            raise SystemExit(f"--reassign needs PATH=GROUP, got {spec!r}")
        reassign.append((path.strip(), grp.strip()))
    out = build(read(args.groups_csv), read(args.lps_csv), args.group, reassign)
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
