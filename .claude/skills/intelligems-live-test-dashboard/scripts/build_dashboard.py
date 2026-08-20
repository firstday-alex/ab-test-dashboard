#!/usr/bin/env python3
"""
Render a self-contained, collapsible HTML dashboard of live Intelligems A/B tests.

Usage:
    python3 build_dashboard.py tests.json -o /mnt/user-data/outputs/intelligems-live-tests.html

Input JSON is described in SKILL.md. Every number in the output comes from the input
file -- this script never invents, estimates, or back-fills a value. Missing metrics
render as an em dash so gaps are visible rather than papered over.
"""

import argparse
import html
import json
import sys
from datetime import datetime

# ---------------------------------------------------------------------------
# Metric registry: the four detail metrics, in display order.
# `fmt` controls rendering; `label` is the column header.
# ---------------------------------------------------------------------------
METRICS = [
    ("conversion_rate", "CVR", "pct"),
    ("net_revenue_per_visitor", "RPV", "money"),
    ("net_revenue_per_order", "AOV", "money"),
    ("add_to_cart_rate", "ATC", "pct"),
]

METRIC_LABELS = {
    "conversion_rate": "CVR",
    "net_revenue_per_visitor": "RPV",
    "net_revenue_per_order": "AOV",
    "add_to_cart_rate": "ATC",
    "gross_profit_per_visitor": "Profit/visitor",
    "n_orders": "Orders",
    "net_revenue": "Net revenue",
}

# Readiness states -> (pill label, css class, sort weight)
READINESS = {
    "call_it": ("Time to make a call", "call", 0),
    "could_decide": ("Could make a decision", "maybe", 1),
    "needs_time": ("Needs time", "wait", 2),
}


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def fmt_value(value, kind):
    if value is None:
        return "&mdash;"
    if kind == "pct":
        return f"{value * 100:.2f}%"
    if kind == "money":
        return f"${value:,.2f}"
    if kind == "int":
        return f"{value:,.0f}"
    return f"{value:,.2f}"


def fmt_lift(uplift):
    """Signed lift with direction class. uplift is a fraction (0.055 -> +5.5%)."""
    if uplift is None:
        return "&mdash;", "flat"
    pct = uplift * 100
    cls = "up" if pct > 0.05 else ("down" if pct < -0.05 else "flat")
    return f"{pct:+.1f}%", cls


def esc(text):
    return html.escape(str(text if text is not None else ""))


# ---------------------------------------------------------------------------
# Derivations
# ---------------------------------------------------------------------------
def control_of(test):
    for v in test["variations"]:
        if v.get("is_control"):
            return v
    return test["variations"][0] if test["variations"] else None


def challengers(test):
    return [v for v in test["variations"] if not v.get("is_control")]


def leader(test):
    """
    The challenger the collapsed row summarizes: highest value on the hero metric.
    Falls back to the first challenger when the hero metric is missing.
    """
    key = (test.get("hero_metric") or {}).get("key")
    cands = challengers(test)
    if not cands:
        return None
    if not key:
        return cands[0]
    scored = [
        (v["metrics"].get(key, {}).get("value"), v)
        for v in cands
        if v.get("metrics", {}).get(key, {}).get("value") is not None
    ]
    if not scored:
        return cands[0]
    return max(scored, key=lambda pair: pair[0])[1]


def validate(test, idx):
    """Fail loudly on structural gaps rather than rendering a misleading row."""
    problems = []
    for field in ("id", "name", "days_live", "readiness", "variations"):
        if test.get(field) in (None, ""):
            problems.append(f"missing '{field}'")
    if test.get("readiness") and test["readiness"] not in READINESS:
        problems.append(
            f"readiness '{test['readiness']}' is not one of {list(READINESS)}"
        )
    if not test.get("variations"):
        problems.append("no variations")
    if problems:
        name = test.get("name") or f"index {idx}"
        raise SystemExit(f"Test '{name}': " + "; ".join(problems))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def render_row(test):
    hero = test.get("hero_metric") or {}
    hero_key = hero.get("key")
    hero_label = hero.get("label") or METRIC_LABELS.get(hero_key, hero_key or "Hero metric")
    if hero.get("assumed"):
        hero_label += "*"

    lead = leader(test)
    ctrl = control_of(test)

    hero_uplift = None
    if lead and hero_key:
        hero_uplift = lead["metrics"].get(hero_key, {}).get("uplift")
    hero_txt, hero_cls = fmt_lift(hero_uplift)

    variant_orders = lead.get("orders") if lead else None
    pill_label, pill_cls, _ = READINESS[test["readiness"]]

    n_var = len(challengers(test))
    lead_note = (
        f' <span class="lead-note">&mdash; leading of {n_var} variants:'
        f' {esc(lead["name"])}</span>'
        if lead and n_var > 1
        else ""
    )

    # ---- detail table -----------------------------------------------------
    head_cells = "".join(f"<th>{lab}</th>" for _, lab, _ in METRICS)
    body_rows = []
    for v in test["variations"]:
        is_ctrl = bool(v.get("is_control"))
        cells = []
        for key, _, kind in METRICS:
            m = v.get("metrics", {}).get(key, {})
            val = fmt_value(m.get("value"), kind)
            if is_ctrl:
                cells.append(f'<td><span class="val">{val}</span></td>')
            else:
                lift_txt, lift_cls = fmt_lift(m.get("uplift"))
                cells.append(
                    f'<td><span class="val">{val}</span>'
                    f'<span class="lift {lift_cls}">{lift_txt}</span></td>'
                )
        tag = '<span class="tag">control</span>' if is_ctrl else ""
        body_rows.append(
            "<tr>"
            f'<th scope="row">{esc(v["name"])}{tag}</th>'
            f'<td class="num">{fmt_value(v.get("visitors"), "int")}</td>'
            f'<td class="num">{fmt_value(v.get("orders"), "int")}</td>'
            + "".join(cells)
            + "</tr>"
        )

    meta_bits = []
    if test.get("started_at"):
        meta_bits.append(f"Started {esc(test['started_at'])}")
    if test.get("traffic_split"):
        meta_bits.append(f"Split {esc(test['traffic_split'])}")
    if test.get("type"):
        meta_bits.append(f"Type {esc(test['type'])}")
    if hero.get("assumed"):
        meta_bits.append(
            f"Hero metric {esc(hero_label.rstrip('*'))} (assumed \u2014 primary not verified)")
    else:
        meta_bits.append(f"Hero metric {esc(hero_label)}")
    if test.get("analytics_view"):
        meta_bits.append(f"View {esc(test['analytics_view'])}")
    if test.get("verdict"):
        meta_bits.append(f"Verdict {esc(test['verdict'])}")

    link = (
        f'<a class="ig-link" href="https://app.intelligems.io/experiment/{esc(test["id"])}"'
        ' target="_blank" rel="noopener">Open in Intelligems &rarr;</a>'
    )

    note = (
        f'<p class="note">{esc(test["note"])}</p>' if test.get("note") else ""
    )

    return f"""
<details class="test" data-readiness="{pill_cls}">
  <summary>
    <span class="chev" aria-hidden="true"></span>
    <span class="row-main">
      <span class="t-name">{esc(test['name'])}{lead_note}</span>
      <span class="stats">
        <span class="stat"><b>{fmt_value(test['days_live'], 'int')}</b><small>days live</small></span>
        <span class="stat"><b>{fmt_value(variant_orders, 'int')}</b><small>variant orders</small></span>
        <span class="stat"><b class="{hero_cls}">{hero_txt}</b><small>{esc(hero_label)}</small></span>
      </span>
      <span class="pill {pill_cls}">{pill_label}</span>
    </span>
  </summary>
  <div class="body">
    <div class="meta">{' &middot; '.join(meta_bits)}</div>
    {note}
    <div class="t-scroll">
    <table>
      <thead><tr><th scope="col">Variation</th><th scope="col">Visitors</th>
      <th scope="col">Orders</th>{head_cells}</tr></thead>
      <tbody>{''.join(body_rows)}</tbody>
    </table>
    </div>
    {link}
  </div>
</details>"""


CSS = """
:root{
  --ink:#11161d; --ink-2:#54616f; --ink-3:#8a95a3;
  --line:#e3e7ec; --bg:#ffffff; --bg-2:#f6f8fa;
  --up:#0f7a4d; --down:#c0392b; --flat:#8a95a3;
  --call:#0f7a4d; --maybe:#b26a00; --wait:#5b6673;
  --radius:10px;
}
*{box-sizing:border-box}
body{margin:0;padding:28px 20px 60px;background:var(--bg-2);color:var(--ink);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:1120px;margin:0 auto}
header{margin-bottom:20px}
h1{margin:0 0 6px;font-size:22px;letter-spacing:-.01em}
.sub{color:var(--ink-2);font-size:13px}
.counts{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0 10px}
.count{background:var(--bg);border:1px solid var(--line);border-radius:999px;
  padding:5px 12px;font-size:12.5px;color:var(--ink-2)}
.count b{color:var(--ink)}
.tools{display:flex;gap:8px;margin:0 0 14px}
.tools button{background:var(--bg);border:1px solid var(--line);border-radius:8px;
  padding:6px 11px;font-size:12.5px;color:var(--ink-2);cursor:pointer;font-family:inherit}
.tools button:hover{border-color:var(--ink-3);color:var(--ink)}

.test{background:var(--bg);border:1px solid var(--line);border-radius:var(--radius);
  margin-bottom:8px;overflow:hidden}
.test[open]{border-color:var(--ink-3);box-shadow:0 1px 3px rgba(17,22,29,.06)}
summary{display:flex;align-items:flex-start;gap:10px;padding:14px 16px;cursor:pointer;list-style:none}
summary::-webkit-details-marker{display:none}
summary::marker{content:""}
summary:hover{background:var(--bg-2)}
.chev{flex:0 0 auto;margin-top:6px;width:0;height:0;border-left:5px solid var(--ink-3);
  border-top:4px solid transparent;border-bottom:4px solid transparent;
  transition:transform .12s ease}
.test[open] .chev{transform:rotate(90deg)}
/* Wrapping flex row: items reflow onto new lines instead of colliding. */
.row-main{flex:1 1 auto;min-width:0;display:flex;flex-wrap:wrap;align-items:center;gap:10px 18px}
.t-name{flex:1 1 220px;min-width:0;font-weight:600;letter-spacing:-.01em;overflow-wrap:anywhere}
.lead-note{font-weight:400;color:var(--ink-3);font-size:12.5px}
.stats{flex:0 1 auto;display:flex;flex-wrap:wrap;gap:8px 0}
/* Fixed-width stats + fixed-width pill keep the columns aligned row to row. */
.stat{flex:0 0 96px;display:flex;flex-direction:column;line-height:1.2;padding-right:10px}
.stat b{font-size:15px;font-variant-numeric:tabular-nums;white-space:nowrap}
.stat small{color:var(--ink-3);font-size:10.5px;text-transform:uppercase;
  letter-spacing:.04em;white-space:nowrap}
.up{color:var(--up)} .down{color:var(--down)} .flat{color:var(--flat)}
.pill{flex:0 0 auto;margin-left:auto;min-width:172px;text-align:center;font-size:12px;
  font-weight:600;padding:5px 11px;border-radius:999px;white-space:nowrap;
  border:1px solid currentColor}
.pill.call{color:var(--call);background:rgba(15,122,77,.07)}
.pill.maybe{color:var(--maybe);background:rgba(178,106,0,.07)}
.pill.wait{color:var(--wait);background:rgba(91,102,115,.07)}

.body{padding:2px 16px 18px;border-top:1px solid var(--line)}
.meta{color:var(--ink-2);font-size:12.5px;padding:12px 0}
.note{margin:0 0 12px;font-size:13px;color:var(--ink-2);background:var(--bg-2);
  border-left:2px solid var(--line);padding:8px 12px;border-radius:0 6px 6px 0}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th,td{text-align:right;padding:9px 10px;border-bottom:1px solid var(--line)}
thead th{font-size:11px;text-transform:uppercase;letter-spacing:.05em;
  color:var(--ink-3);font-weight:600;border-bottom:1px solid var(--line)}
thead th:first-child,tbody th{text-align:left}
tbody th{font-weight:600}
tbody tr:last-child th,tbody tr:last-child td{border-bottom:none}
.tag{font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--ink-3);
  margin-left:7px;font-weight:600}
td .val{font-variant-numeric:tabular-nums}
td .lift{display:block;font-size:11.5px;font-variant-numeric:tabular-nums}
.ig-link{display:inline-block;margin-top:14px;font-size:12.5px;color:var(--ink-2);text-decoration:none}
.ig-link:hover{color:var(--ink);text-decoration:underline}
.t-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
.empty{background:var(--bg);border:1px solid var(--line);border-radius:var(--radius);
  padding:32px;text-align:center;color:var(--ink-2)}
footer{margin-top:24px;color:var(--ink-3);font-size:12px}
@media (max-width:560px){
  body{padding:18px 12px 40px}
  table{font-size:12.5px;min-width:520px} th,td{padding:8px 7px}
  .pill{margin-left:0}
}
"""

JS = """
document.querySelectorAll('[data-act]').forEach(function(b){
  b.addEventListener('click', function(){
    var open = b.dataset.act === 'open';
    document.querySelectorAll('details.test').forEach(function(d){ d.open = open; });
  });
});
"""


def build(data):
    tests = data.get("tests", [])
    for i, t in enumerate(tests):
        validate(t, i)

    tests = sorted(
        tests,
        key=lambda t: (READINESS[t["readiness"]][2], -float(t.get("days_live") or 0)),
    )

    tallies = {k: 0 for k in READINESS}
    for t in tests:
        tallies[t["readiness"]] += 1

    counts = "".join(
        f'<span class="count"><b>{tallies[k]}</b> {READINESS[k][0].lower()}</span>'
        for k in ("call_it", "could_decide", "needs_time")
        if tallies[k]
    )

    generated = data.get("generated_at") or datetime.now().astimezone().isoformat(
        timespec="minutes"
    )
    org = data.get("organization")
    org_txt = f" &middot; {esc(org)}" if org else ""

    rows = (
        "".join(render_row(t) for t in tests)
        if tests
        else '<div class="empty">No A/B tests are currently live in Intelligems.</div>'
    )

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Live A/B Tests &middot; Intelligems</title>
<style>{CSS}</style>
</head><body>
<div class="wrap">
  <header>
    <h1>Live A/B Tests</h1>
    <div class="sub">{len(tests)} test{'' if len(tests)==1 else 's'} running{org_txt}
      &middot; data pulled {esc(generated)}</div>
    <div class="counts">{counts}</div>
    <div class="tools">
      <button data-act="open" type="button">Expand all</button>
      <button data-act="close" type="button">Collapse all</button>
    </div>
  </header>
  {rows}
  <footer>Figures are live and will move while tests are running. Lift shown vs. control.
    All values pulled from the Intelligems API at the time above.</footer>
</div>
<script>{JS}</script>
</body></html>"""


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", help="Path to the pulled test data JSON")
    ap.add_argument("-o", "--output", required=True, help="Path for the HTML dashboard")
    args = ap.parse_args()

    with open(args.input) as fh:
        data = json.load(fh)

    with open(args.output, "w") as fh:
        fh.write(build(data))

    n = len(data.get("tests", []))
    print(f"Wrote {args.output} ({n} live test{'' if n == 1 else 's'})")


if __name__ == "__main__":
    sys.exit(main())
