#!/usr/bin/env python3
"""
Render the dashboard at 380, 760 and 1180px and fail if any summary-row elements
overlap or the page scrolls sideways. CLAUDE.md requires this before every push:
the layout once broke in a way every text-based check passed.

    python3 visual_check.py index.html [--shots DIR]

Needs `pip install playwright && playwright install chromium`.
"""
import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

WIDTHS = (380, 760, 1180)
BOXES_JS = """els => els.map(s => [...s.querySelectorAll('.stat, .t-name, .pill')].map(e => {
  const r = e.getBoundingClientRect(); return {t: r.top, b: r.bottom, l: r.left, rt: r.right}; }))"""


def overlaps(a, b, eps=0.5):
    return a["l"] < b["rt"] - eps and b["l"] < a["rt"] - eps and a["t"] < b["b"] - eps and b["t"] < a["b"] - eps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("html")
    ap.add_argument("--shots", help="directory to save full-page screenshots")
    a = ap.parse_args()
    url = Path(a.html).resolve().as_uri()
    problems = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for w in WIDTHS:
            page = browser.new_page(viewport={"width": w, "height": 900})
            page.goto(url)
            rows = page.eval_on_selector_all("summary", BOXES_JS)
            for i, boxes in enumerate(rows):
                for x in range(len(boxes)):
                    for y in range(x + 1, len(boxes)):
                        if overlaps(boxes[x], boxes[y]):
                            problems.append(f"{w}px: row {i + 1}, elements {x} and {y} overlap")
            sw = page.evaluate("document.documentElement.scrollWidth")
            if sw > w:
                problems.append(f"{w}px: page scrolls sideways (scrollWidth {sw})")
            if a.shots:
                Path(a.shots).mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(Path(a.shots) / f"dashboard-{w}.png"), full_page=True)
            print(f"{w}px: {len(rows)} rows checked")
            page.close()
        browser.close()
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print("visual check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
