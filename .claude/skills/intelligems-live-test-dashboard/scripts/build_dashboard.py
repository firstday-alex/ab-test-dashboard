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
import re
import sys
from datetime import datetime

# ---------------------------------------------------------------------------
# Metric registry: the detail metrics, in display order.
#
# `fmt` controls rendering; `label` is the column header. `higher_is_better`
# drives the lift COLOUR only -- the printed sign always follows the data. It
# exists because abandonment rates run the other way: +4% cart abandonment is a
# regression, and colouring it green because the number went up is exactly the
# kind of quiet misread this dashboard is supposed to prevent.
# ---------------------------------------------------------------------------
METRICS = [
    ("conversion_rate", "CVR", "pct", True),
    ("net_revenue_per_visitor", "RPV", "money", True),
    ("net_revenue_per_order", "AOV", "money", True),
    ("add_to_cart_rate", "ATC", "pct", True),
    ("abandoned_cart_rate", "Cart aband.", "pct", False),
    ("checkout_completion_rate", "Checkout compl.", "pct", True),
    # Pulled separately with filters.customerType="new", which narrows orders
    # (visitors are unchanged), so this is pct_subscription_orders among
    # new-customer orders only.
    ("new_customer_pct_subscription_orders", "New-cust. sub %", "pct", True),
]

# Metrics computed here rather than pulled, as the exact complement of a metric
# the API does return: {derived key -> source key}. See metric_cell().
COMPLEMENT_OF = {
    "checkout_completion_rate": "abandoned_checkout_rate",
}

METRIC_LABELS = {
    "conversion_rate": "CVR",
    "net_revenue_per_visitor": "RPV",
    "net_revenue_per_order": "AOV",
    "add_to_cart_rate": "ATC",
    "abandoned_cart_rate": "Cart abandonment",
    "abandoned_checkout_rate": "Checkout abandonment",
    "checkout_completion_rate": "Checkout completion",
    "new_customer_pct_subscription_orders": "New-customer subscription %",
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
# Traffic groups
#
# A test's name carries its landing-page groups in square brackets, and a test
# may span several: "[TDK-BTS][KDE-BTS] Name" covers both. Sessions per group
# come from `traffic.groups` in the input JSON.
#
# Per test, traffic is the SUM over that test's own groups. Across tests it is
# the UNION of groups, never the sum -- two tests on the same group must not
# count that group's sessions twice. Summing instead of unioning is how a
# coverage figure ends up above 100%.
# ---------------------------------------------------------------------------
# Deep link into the Intelligems app. Override with `intelligems_url_template`
# in the input JSON -- the app is a single-page app that returns 200 for every
# path, so a wrong template cannot be detected by fetching it.
IG_URL_TEMPLATE = "https://app.intelligems.io/experiment/{id}"

# The Google Sheet behind `traffic.groups`, linked from the landing-page groups
# menu. The published link is view-only; Google gives no way to derive the
# editable /d/<id>/edit URL from a /d/e/2PACX-... publish ID. Set
# `traffic.sheet_edit_url` in the input JSON (or SHEET_EDIT_URL here) to make the
# button an edit link -- until then it is labelled as view-only, not as "Edit".
SHEET_VIEW_URL = (
    "https://docs.google.com/spreadsheets/d/e/"
    "2PACX-1vR-UNFL2QOO463D9C72xV0GZX5BD_WYTPPzXInUkZ2cuiuS0n6--G-rKKz7IZ2eZCsWwETiVQ9PJTFd/pubhtml"
)
SHEET_EDIT_URL = None


def ig_url(test, template=None):
    return (template or IG_URL_TEMPLATE).format(id=test["id"])


def norm_group(name):
    return " ".join(str(name).split()).strip().upper()


def traffic_index(data):
    """
    {normalized group -> (display name, sessions)} plus the denominator.

    `traffic.sitewide_groups` names groups that span the whole denominator -- a
    cart drawer or header is on every session, so it isn't a landing-page
    surface. They resolve to `total_sessions`, which makes them 100% by
    construction and lets the 100% rule below catch them without a second flag.
    """
    traffic = data.get("traffic") or {}
    groups = dict(traffic.get("groups") or {})
    total = traffic.get("total_sessions")
    total = float(total) if total is not None else sum(float(v) for v in groups.values())
    for name in traffic.get("sitewide_groups") or []:
        groups.setdefault(name, total)
    idx = {norm_group(k): (k, float(v)) for k, v in groups.items()}
    return idx, total


def is_sitewide(sessions, total):
    """
    A group covering 100% of traffic is sitewide, not a surface.

    Such a group tells you nothing about which surfaces are being tested, so it
    shows on its own row but is excluded from the coverage roll-up and from the
    opportunities list. Counting it would peg coverage at 100% and hide the gaps
    the roll-up exists to expose.
    """
    return bool(total) and sessions >= total - 0.5


def test_groups(test):
    """Groups this test claims, in name order. Explicit `groups` wins if present."""
    if test.get("groups") is not None:
        return [str(g).strip() for g in test["groups"] if str(g).strip()]
    return [m.strip() for m in re.findall(r"\[([^\[\]]+)\]", test.get("name") or "")]


def resolve_traffic(test, idx, denom=0.0):
    """
    -> (sessions or None, matched display names, unmatched names, sitewide flag)

    Three outcomes, and the difference between the last two matters:

      groups matched          -> sum of their sessions
      groups claimed, none in
      the sheet               -> 0.0. The sheet is the complete record of the
                                 period's sessions, so a group absent from it
                                 drew none. The test is still live and belongs
                                 on the board at 0%.
      no group tag at all     -> None, rendered as an em dash. Nothing was
                                 claimed, so nothing is known -- that is not the
                                 same as zero.

    A sitewide group subsumes every other group, so the row is the whole
    denominator rather than a sum that would exceed it.
    """
    matched, missing, total, wide = [], [], 0.0, False
    for g in test_groups(test):
        key = norm_group(g)
        if key in idx:
            display, sessions = idx[key]
            if display not in matched:      # a name repeated in one title counts once
                matched.append(display)
                total += sessions
                if is_sitewide(sessions, denom):
                    wide = True
        else:
            missing.append(g)
    if wide:
        total = denom
    if not matched and not missing:
        return None, matched, missing, wide
    return total, matched, missing, wide


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


def fmt_lift(uplift, higher_is_better=True):
    """
    Signed lift with a direction class. uplift is a fraction (0.055 -> +5.5%).

    The printed number always carries the true sign. `higher_is_better=False`
    only swaps the good/bad colour, for metrics like abandonment where a rise is
    a regression.
    """
    if uplift is None:
        return "&mdash;", "flat"
    pct = uplift * 100
    if abs(pct) <= 0.05:
        return f"{pct:+.1f}%", "flat"
    good = (pct > 0) if higher_is_better else (pct < 0)
    return f"{pct:+.1f}%", "up" if good else "down"


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


def metric_cell(v, key, ctrl=None):
    """
    -> {"value":..., "uplift":...} for `key` on variation `v`, or {} if absent.

    Most keys are read straight off the pull. A key in COMPLEMENT_OF is the
    exact complement of one the API does return -- checkout completion is
    1 - abandoned_checkout_rate, an identity on a pulled number rather than an
    estimate -- so it is derived here instead of being stored. That keeps the
    input JSON a faithful record of what Intelligems actually returned, and
    keeps the arithmetic in one reviewable place.

    The lift of a complement has to be recomputed from the two complement
    values: it is NOT the negated lift of the source metric. Going from 49.75%
    to 50.22% abandonment is +0.94%, while the matching completion move is
    -0.94% only because the rate happens to sit near 50%. Away from 50% the two
    diverge, so the API's uplift on the source is not reusable here. With no
    control value to compare against, the lift stays absent and renders as an
    em dash, same as any other missing number.
    """
    src = COMPLEMENT_OF.get(key)
    if not src:
        return (v.get("metrics") or {}).get(key) or {}

    val = ((v.get("metrics") or {}).get(src) or {}).get("value")
    out = {"value": None if val is None else 1.0 - val}
    if ctrl is not None and v is not ctrl and val is not None:
        cval = ((ctrl.get("metrics") or {}).get(src) or {}).get("value")
        if cval is not None and cval != 1.0:
            out["uplift"] = (1.0 - val) / (1.0 - cval) - 1.0
    return out


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
        (metric_cell(v, key).get("value"), v)
        for v in cands
        if metric_cell(v, key).get("value") is not None
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
def render_row(test, tidx=None, ttotal=0.0, url_tpl=None):
    hero = test.get("hero_metric") or {}
    hero_key = hero.get("key")
    hero_label = hero.get("label") or METRIC_LABELS.get(hero_key, hero_key or "Hero metric")
    if hero.get("assumed"):
        hero_label += "*"

    lead = leader(test)
    ctrl = control_of(test)

    hero_uplift = None
    if lead and hero_key:
        hero_uplift = metric_cell(lead, hero_key, ctrl).get("uplift")
    # A test primaried on an abandonment metric reads the other way; look the
    # direction up rather than assuming up == good on the collapsed row too.
    hero_better = next(
        (hb for k, _, _, hb in METRICS if k == hero_key), True
    )
    hero_txt, hero_cls = fmt_lift(hero_uplift, hero_better)

    variant_orders = lead.get("orders") if lead else None
    pill_label, pill_cls, _ = READINESS[test["readiness"]]

    n_var = len(challengers(test))
    lead_note = (
        f' <span class="lead-note">&mdash; leading of {n_var} variants:'
        f' {esc(lead["name"])}</span>'
        if lead and n_var > 1
        else ""
    )

    # ---- traffic: sum of this test's own groups ---------------------------
    sessions, matched, missing, wide = resolve_traffic(test, tidx or {}, ttotal)
    if sessions is None:
        traffic_stat = '<span class="stat"><b>&mdash;</b><small>traffic</small></span>'
    else:
        share = f"{sessions / ttotal * 100:.1f}%" if ttotal else "&mdash;"
        # "sitewide" replaces the label rather than extending it: the stat boxes are
        # a fixed 96px so the columns line up row to row, and a longer string widens
        # this one box (auto min-width beats flex-basis) and shifts the whole row.
        label = "sitewide" if wide else "traffic"
        traffic_stat = (
            f'<span class="stat"><b>{share}</b><small>{label}</small></span>'
        )

    # ---- detail table -----------------------------------------------------
    head_cells = "".join(f"<th>{lab}</th>" for _, lab, _, _ in METRICS)
    body_rows = []
    for v in test["variations"]:
        is_ctrl = bool(v.get("is_control"))
        cells = []
        for key, _, kind, higher_better in METRICS:
            # An absent key and an explicit null both mean "not returned" -- the
            # JSON spec allows either, and both must render as an em dash.
            m = metric_cell(v, key, ctrl)
            val = fmt_value(m.get("value"), kind)
            if is_ctrl:
                cells.append(f'<td><span class="val">{val}</span></td>')
            else:
                lift_txt, lift_cls = fmt_lift(m.get("uplift"), higher_better)
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
    if matched:
        share = f" ({sessions / ttotal * 100:.1f}%)" if ttotal else ""
        meta_bits.append(
            f"Groups {esc(' + '.join(matched))} &rarr; {sessions:,.0f} sessions{share}"
        )
    if wide:
        meta_bits.append(
            "Sitewide &mdash; on every session, so excluded from the coverage roll-up"
        )
    if missing:
        # Keep this visible: on the collapsed row, "0% because the group drew no
        # sessions" and "0% because the group name is a typo" look identical.
        meta_bits.append(
            f"Group{'s' if len(missing) > 1 else ''} {esc(', '.join(missing))}"
            " not in the traffic sheet &mdash; counted as 0%"
        )
    if not matched and not missing:
        meta_bits.append("No group tag — traffic share unknown")

    link = (
        f'<a class="ig-link" href="{esc(ig_url(test, url_tpl))}"'
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
      <span class="t-name"><a class="t-link" href="{ig_url(test, url_tpl)}"
        target="_blank" rel="noopener"
        title="Open in Intelligems (new tab)">{esc(test['name'])}</a>{lead_note}</span>
      <span class="stats">
        <span class="stat"><b>{fmt_value(test['days_live'], 'int')}</b><small>days live</small></span>
        <span class="stat"><b>{fmt_value(variant_orders, 'int')}</b><small>variant orders</small></span>
        <span class="stat"><b class="{hero_cls}">{hero_txt}</b><small>{esc(hero_label)}</small></span>
        {traffic_stat}
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
/* Test name is a deep link; the row still expands when clicked elsewhere. */
.t-link{color:inherit;text-decoration:none;border-bottom:1px solid transparent}
.t-link:hover{color:var(--up);border-bottom-color:currentColor}
.t-link:focus-visible{outline:2px solid var(--up);outline-offset:2px;border-radius:2px}
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
/* Pull stamp repeated at the page foot: on a long page the header stamp
   scrolls away, and a stale dashboard that looks current is the failure mode. */
.stamp{color:var(--ink-2);padding:9px 12px;margin-bottom:8px;background:var(--bg);
  border:1px solid var(--line);border-radius:8px;overflow-wrap:anywhere}
.stamp b{color:var(--ink);font-variant-numeric:tabular-nums}

/* Coverage pill in the header counts row. */
.count.cov{background:var(--bg);border-color:var(--ink-3);color:var(--ink-2)}
.cov-note{color:var(--ink-3)}

/* Top traffic opportunities. */
.opps{background:var(--bg);border:1px solid var(--line);border-radius:var(--radius);
  padding:16px 16px 6px;margin-top:20px}
.opps h2{margin:0 0 4px;font-size:15px;letter-spacing:-.01em}
.opps-sub{margin:0 0 10px;font-size:12.5px;color:var(--ink-2)}
.opps table{font-size:13.5px}
.opps tbody th{font-weight:600}

/* Landing-page groups menu: hamburger in the header, panel slides in from the right. */
.h-top{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}
.burger{flex:0 0 auto;width:38px;height:38px;border:1px solid var(--line);border-radius:8px;
  background:var(--bg);cursor:pointer;display:flex;align-items:center;justify-content:center}
.burger:hover{border-color:var(--ink-3)}
.burger span,.burger span::before,.burger span::after{display:block;width:16px;height:2px;
  background:var(--ink-2);border-radius:1px;position:relative}
.burger span::before,.burger span::after{content:"";position:absolute;left:0}
.burger span::before{top:-5px} .burger span::after{top:5px}
.lk-backdrop{position:fixed;inset:0;background:rgba(17,22,29,.28);z-index:20}
.lk{position:fixed;top:0;right:0;bottom:0;width:min(440px,100vw);z-index:21;overflow-y:auto;
  background:var(--bg);border-left:1px solid var(--line);box-shadow:-4px 0 18px rgba(17,22,29,.12);
  padding:18px 16px 24px}
.lk[hidden],.lk-backdrop[hidden]{display:none}
.lk-head{display:flex;align-items:center;justify-content:space-between;gap:10px}
.lk h2{margin:0;font-size:16px;letter-spacing:-.01em}
.lk h3{margin:18px 0 4px;font-size:13.5px}
.lk-x{border:none;background:none;font-size:24px;line-height:1;color:var(--ink-2);cursor:pointer;
  padding:4px 8px;border-radius:6px}
.lk-x:hover{background:var(--bg-2);color:var(--ink)}
.lk-sub{margin:6px 0 10px;font-size:12.5px;color:var(--ink-2)}
.lk-table{font-size:13px} .lk-table th,.lk-table td{padding:8px 6px}
.lk-table tbody th{white-space:nowrap}
.lk-table thead th:last-child,.lk-tests{text-align:left}
.lk-tests{font-size:12px;color:var(--ink-2);overflow-wrap:anywhere}
.lk-miss{margin:0;padding-left:18px;font-size:12.5px;color:var(--ink-2)}
.lk-foot{margin-top:18px;padding-top:14px;border-top:1px solid var(--line)}
.lk-btn{display:inline-block;background:var(--ink);color:#fff;text-decoration:none;
  border-radius:8px;padding:8px 14px;font-size:13px;font-weight:600}
.lk-btn:hover{background:#2a3440}
body.lk-open{overflow:hidden}

@media (max-width:560px){
  .lk-table{min-width:0}
  body{padding:18px 12px 40px}
  /* 9 columns now; below this the table scrolls inside .t-scroll rather than
     letting the numbers crush together. Raise with any new column. */
  table{font-size:12.5px;min-width:790px} th,td{padding:8px 7px}
  .opps table{min-width:0}
  .pill{margin-left:0}
}
"""

JS = """
/* A click on the name opens Intelligems in a new tab; stopping propagation
   keeps the <summary> from also toggling the row open/closed. */
document.querySelectorAll('summary .t-link').forEach(function(a){
  a.addEventListener('click', function(e){ e.stopPropagation(); });
});
document.querySelectorAll('[data-act]').forEach(function(b){
  b.addEventListener('click', function(){
    var open = b.dataset.act === 'open';
    document.querySelectorAll('details.test').forEach(function(d){ d.open = open; });
  });
});
/* Landing-page groups menu. Closes on the X, the backdrop, or Escape, and hands
   focus back to the hamburger so keyboard users land where they started. */
(function(){
  var panel = document.getElementById('lk-panel');
  var back = document.querySelector('.lk-backdrop');
  var burger = document.querySelector('.burger');
  if (!panel || !burger) return;
  function set(open){
    panel.hidden = !open; back.hidden = !open;
    burger.setAttribute('aria-expanded', open ? 'true' : 'false');
    document.body.classList.toggle('lk-open', open);
    if (open) panel.querySelector('.lk-x').focus(); else burger.focus();
  }
  document.querySelectorAll('[data-lk]').forEach(function(el){
    el.addEventListener('click', function(){ set(el.dataset.lk === 'open'); });
  });
  document.addEventListener('keydown', function(e){
    if (e.key === 'Escape' && !panel.hidden) set(false);
  });
})();
"""


def coverage(tests, tidx, ttotal):
    """
    Deduped traffic coverage across all live tests.

    The union is the whole point: two tests on the same group cover that group's
    sessions once, not twice. Summing per-test traffic instead would exceed 100%
    the moment two tests share a group.
    """
    under_test = set()
    for t in tests:
        _, matched, _, _ = resolve_traffic(t, tidx, ttotal)
        for m in matched:
            key = norm_group(m)
            if not is_sitewide(tidx[key][1], ttotal):
                under_test.add(key)

    tested = sum(tidx[k][1] for k in under_test)
    idle = sorted(
        (
            (disp, sess)
            for k, (disp, sess) in tidx.items()
            if k not in under_test and not is_sitewide(sess, ttotal)
        ),
        key=lambda pair: -pair[1],
    )
    return tested, under_test, idle


def render_coverage(tested, ttotal, n_groups, label):
    if not ttotal:
        return ""
    pct = tested / ttotal * 100
    return (
        f'<span class="count cov"><b>{pct:.1f}%</b> of {esc(label)} under test'
        f' <span class="cov-note">({n_groups} group{"s" if n_groups != 1 else ""},'
        f' deduplicated)</span></span>'
    )


def render_opportunities(idle, ttotal, label, source):
    """Groups carrying traffic with no live test on them, largest first."""
    if not idle:
        return ""
    rows = "".join(
        f"<tr><th scope=\"row\">{esc(name)}</th>"
        f'<td class="num">{sessions:,.0f}</td>'
        f'<td class="num">{(sessions / ttotal * 100) if ttotal else 0:.1f}%</td></tr>'
        for name, sessions in idle
    )
    total_idle = sum(s for _, s in idle)
    share = f"{total_idle / ttotal * 100:.1f}%" if ttotal else "&mdash;"
    return f"""
<section class="opps">
  <h2>Top traffic opportunities</h2>
  <p class="opps-sub">Landing-page groups with traffic and no live test on them &mdash;
    {share} of {esc(label)} going untested. Source: {esc(source)}.</p>
  <div class="t-scroll">
  <table>
    <thead><tr><th scope="col">Group</th><th scope="col">Sessions</th>
    <th scope="col">Share</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
  </div>
</section>"""


def render_lookup(tests, tidx, ttotal, label, source, traffic_meta):
    """
    Slide-out menu listing the landing-page groups exactly as the sheet has them.

    Read-only by design: the sheet is the source of truth, so changes are made
    there and picked up on the next refresh. The menu links out to it rather
    than offering an in-page editor whose edits would have nowhere to persist.
    """
    used_by, unmatched = {}, []
    for t in tests:
        _, matched, missing, _ = resolve_traffic(t, tidx, ttotal)
        for m in matched:
            used_by.setdefault(norm_group(m), []).append(t["name"])
        for g in missing:
            unmatched.append((g, t["name"]))

    rows = "".join(
        f'<tr><th scope="row">{esc(disp)}</th>'
        f'<td class="num">{sess:,.0f}</td>'
        f'<td class="num">{(sess / ttotal * 100) if ttotal else 0:.1f}%</td>'
        f'<td class="lk-tests">{"<br>".join(esc(n) for n in used_by.get(k, [])) or "&mdash;"}</td></tr>'
        for k, (disp, sess) in sorted(tidx.items(), key=lambda kv: -kv[1][1])
    )
    if not rows:
        rows = '<tr><td colspan="4">No traffic groups in this pull.</td></tr>'

    miss = ""
    if unmatched:
        items = "".join(
            f"<li><b>{esc(g)}</b> &mdash; {esc(n)}</li>" for g, n in unmatched
        )
        miss = (
            '<h3>Test tags not in the sheet</h3>'
            '<p class="lk-sub">Counted as 0% traffic until the sheet has a group'
            f' with the same name.</p><ul class="lk-miss">{items}</ul>'
        )

    edit_url = traffic_meta.get("sheet_edit_url") or SHEET_EDIT_URL
    view_url = traffic_meta.get("sheet_view_url") or SHEET_VIEW_URL
    if edit_url:
        btn = (f'<a class="lk-btn" href="{esc(edit_url)}" target="_blank"'
               ' rel="noopener">Edit in Google Sheets &#8599;</a>')
    elif view_url:
        btn = (f'<a class="lk-btn" href="{esc(view_url)}" target="_blank"'
               ' rel="noopener">Open Google Sheet &#8599;</a>'
               '<p class="lk-sub">This opens the published, view-only copy.'
               ' Edits are made in the source sheet.</p>')
    else:
        btn = ""

    return f"""
<div class="lk-backdrop" data-lk="close" hidden></div>
<aside class="lk" id="lk-panel" aria-label="Landing-page groups" hidden>
  <div class="lk-head">
    <h2>Landing-page groups</h2>
    <button class="lk-x" type="button" data-lk="close" aria-label="Close menu">&times;</button>
  </div>
  <p class="lk-sub">Read from the Google Sheet as of this pull. Source: {esc(source)}.
    Shares are of {esc(label)}.</p>
  <div class="t-scroll">
  <table class="lk-table">
    <thead><tr><th scope="col">Group</th><th scope="col">Sessions</th>
    <th scope="col">Share</th><th scope="col">Live tests</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
  </div>
  {miss}
  <div class="lk-foot">{btn}
    <p class="lk-sub">Changes in the sheet show up at the next dashboard refresh.</p></div>
</aside>"""


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

    url_tpl = data.get("intelligems_url_template")
    traffic_meta = data.get("traffic") or {}
    tidx, ttotal = traffic_index(data)
    tlabel = traffic_meta.get("denominator_label") or "sessions"
    # Every template appends its own full stop; a source that ends in one would double it.
    tsource = (traffic_meta.get("source") or "unspecified").rstrip(". ")
    tested, under_test, idle = coverage(tests, tidx, ttotal)
    counts += render_coverage(tested, ttotal, len(under_test), tlabel)
    opps = render_opportunities(idle, ttotal, tlabel, tsource)
    lookup = render_lookup(tests, tidx, ttotal, tlabel, tsource, traffic_meta)

    generated = data.get("generated_at") or datetime.now().astimezone().isoformat(
        timespec="minutes"
    )
    org = data.get("organization")
    org_txt = f" &middot; {esc(org)}" if org else ""

    rows = (
        "".join(render_row(t, tidx, ttotal, url_tpl) for t in tests)
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
    <div class="h-top">
      <h1>Live A/B Tests</h1>
      <button class="burger" type="button" data-lk="open" aria-controls="lk-panel"
        aria-expanded="false" aria-label="Landing-page groups menu"><span></span></button>
    </div>
    <div class="sub">{len(tests)} test{'' if len(tests)==1 else 's'} running{org_txt}
      &middot; data pulled {esc(generated)}</div>
    <div class="counts">{counts}</div>
    <div class="tools">
      <button data-act="open" type="button">Expand all</button>
      <button data-act="close" type="button">Collapse all</button>
    </div>
  </header>
  {rows}
  {opps}
  <footer>
    <div class="stamp"><b>Last pulled {esc(generated)}</b> &mdash; Intelligems test metrics.
      Traffic groups: {esc(tsource)}.</div>
    Figures are live and will move while tests are running. Lift shown vs. control.
    Cart abandonment is added-to-cart visitors who never began checkout; higher is worse, so
    its lift is coloured by direction rather than by sign. Checkout completion is the
    complement of Intelligems' abandoned-checkout rate (1 &minus; rate), and its lift is
    computed from the two completion values, not carried over from the abandonment metric.
    New-cust. sub % is the share of new-customer orders that include a subscription,
    from a second Intelligems pull filtered to new customers.
  </footer>
</div>
{lookup}
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
