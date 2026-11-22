#!/usr/bin/env python3
"""
Pull every live Intelligems A/B test straight from the Intelligems External API
and write the JSON that build_dashboard.py renders. No MCP, no chat session.

    INTELLIGEMS_API_KEY=... python3 pull_intelligems.py -o live_tests.json

API: https://api.intelligems.io/v25-10-beta (beta). Auth header
`intelligems-access-token`; the key is created in Intelligems under
Integrations > Intelligems External API. Read-only: this script only ever
issues GET experiences-list / GET experiences/{id} / POST analytics/resource/{id}
(a read despite the verb -- the filters travel in the body). It never calls the
create, update or start/pause/end endpoints.

Per live test, the same three reads the skill makes through the MCP:
  1. GET  /experiences/{id}                      -> primary key metric
  2. POST /analytics/resource/{id} testResult    -> every metric + verdict
  3. POST /analytics/resource/{id} customerType  -> new-customer subscription %

The data checks from CLAUDE.md ("Check the data before publishing") run here and
write what they find into each test's `note`, so an unattended refresh never
publishes a bogus lift without comment. Judgement calls the checks can't make --
a group override, context only a person knows -- live in overrides.json, keyed
by experience id, and are appended to the generated note.

Every number in the output comes from an API response made by this run.
"""

import argparse
import csv
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import traffic_breakdown  # noqa: E402  (same directory)

API = "https://api.intelligems.io/v25-10-beta"
SHEET = ("https://docs.google.com/spreadsheets/d/e/2PACX-1vR-UNFL2QOO463D9C72xV0GZX5BD_"
         "WYTPPzXInUkZ2cuiuS0n6--G-rKKz7IZ2eZCsWwETiVQ9PJTFd/pub")
SHEET_GROUPS = SHEET + "?output=csv"
SHEET_LPS = SHEET + "?gid=2032858919&single=true&output=csv"
# The sheet covers one day (yesterday). Verified against Shopify sessions by
# utm_content on 2026-11-16 and 2026-11-19; the sheet itself doesn't say so.
SHEET_WINDOW_DAYS = 1

PRIMARY_MAP = {
    "conversionRate": ("conversion_rate", "CVR"),
    "netRevenuePerVisitor": ("net_revenue_per_visitor", "RPV"),
    "netRevenuePerOrder": ("net_revenue_per_order", "AOV"),
    "addToCartRate": ("add_to_cart_rate", "ATC"),
    "profitPerVisitor": ("gross_profit_per_visitor", "Profit/visitor"),
}
METRIC_KEYS = ["conversion_rate", "net_revenue_per_visitor", "net_revenue_per_order",
               "add_to_cart_rate", "abandoned_cart_rate", "abandoned_checkout_rate",
               "gross_profit_per_visitor"]
READINESS = {
    "not_ready": "needs_time",
    "directional_win": "could_decide", "directional_loss": "could_decide",
    "mixed_signals": "could_decide",
    "strong_win": "call_it", "strong_loss": "call_it",
}
FUNNEL_FOR_AA = ["view_product_page_rate", "add_to_cart_rate", "checkout_begin_rate",
                 "conversion_rate", "net_revenue_per_visitor", "net_revenue_per_order"]

# Thresholds for the automated data checks.
TINY_ORDERS = 30            # below this in any arm, lifts are noise
IMBALANCE_REL = 0.10        # >10% off the configured share of visitors
IMBALANCE_MIN_VISITORS = 1000
SIGN_CONFLICT_MIN = 0.02    # CVR and AOV each moving at least 2%, opposite ways
IMPACT_MENTION = 10000      # mention Intelligems' $/month estimate above this
AA_SIG = 0.95               # p2bc at/above this (or at/below 1-this) on an A/A


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class Api:
    def __init__(self, key, cache_dir=None):
        self.key = key
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def call(self, method, path, body=None, params=None, tag=None):
        url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(6):
            req = urllib.request.Request(url, data=data, method=method, headers={
                "intelligems-access-token": self.key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "ab-test-dashboard-refresh",
            })
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    out = json.load(r)
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 5:
                    wait = int(e.headers.get("retry-after") or 15)
                    print(f"  rate limited on {path}; waiting {wait}s", file=sys.stderr)
                    time.sleep(wait + 1)
                    continue
                if e.code >= 500 and attempt < 3:
                    time.sleep(5 * (attempt + 1))
                    continue
                detail = e.read().decode(errors="replace")[:500]
                raise SystemExit(f"{method} {path} -> HTTP {e.code}: {detail}")
        if self.cache_dir and tag:
            (self.cache_dir / f"{tag}.json").write_text(json.dumps(out, indent=1))
        return out


def fetch_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "ab-test-dashboard-refresh"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def parse_ts(v):
    """Unix ms (what the REST docs specify), Unix s, or ISO 8601 -> aware datetime."""
    if v is None:
        return None
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
        n = float(v)
        return datetime.fromtimestamp(n / 1000 if n > 1e11 else n, tz=timezone.utc)
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def val(metric):
    return metric.get("value") if isinstance(metric, dict) else None


def up(metric):
    if not isinstance(metric, dict):
        return None
    u = metric.get("uplift")
    return u.get("value") if isinstance(u, dict) else None


def pct(x, digits=1):
    return f"{x * 100:+.{digits}f}%"


def chance(p):
    return f"{p * 100:.0f}%" if p is not None else "n/a"


def money(n):
    return f"{'+' if n >= 0 else '-'}${abs(n):,.0f}"


def path_of(url):
    return urllib.parse.urlparse(url).path if url else None


# ---------------------------------------------------------------------------
# Pull
# ---------------------------------------------------------------------------
def list_live(api):
    out, page = [], 1
    while True:
        r = api.call("GET", "/experiences-list", params={
            "status": "started", "category": "experiment", "limit": 100, "page": page},
            tag=f"list-p{page}")
        out += r.get("experiencesList") or []
        if page >= int(r.get("totalPages") or 1):
            return out
        page += 1


def primary_metric(detail):
    exp = detail.get("experience", detail)
    km = exp.get("experienceKeyMetrics") or []
    prim = [m for m in km if m.get("isPrimary")]
    if len(prim) != 1:
        return None, f"{len(prim)} primary key metrics configured"
    sid = prim[0].get("standardEventId")
    if sid in PRIMARY_MAP:
        return PRIMARY_MAP[sid], None
    return None, f"primary metric is '{sid or 'a custom metric'}', which this dashboard doesn't show"


def by_variation(resp):
    return {m.get("variation_id"): m for m in resp.get("metrics") or []}


def build_test(item, detail, overview, newcust, now):
    tid = item["id"]
    variations = sorted(overview.get("variations") or item.get("variations") or [],
                        key=lambda v: v.get("order", 0))
    mets, nmets = by_variation(overview), by_variation(newcust)
    started = parse_ts(item.get("startedAtTs"))
    days = int((now - started).total_seconds() // 86400) if started else 0

    hero, why = primary_metric(detail)
    hero_metric = ({"key": hero[0], "label": hero[1]} if hero else
                   {"key": "net_revenue_per_visitor", "label": "RPV", "assumed": True})

    tr = overview.get("testResult") or {}
    verdict = tr.get("verdict")
    readiness = READINESS.get(verdict, "needs_time")

    rows = []
    for v in variations:
        m = mets.get(v["id"], {})
        n = nmets.get(v["id"], {})
        metrics = {}
        for k in METRIC_KEYS:
            if val(m.get(k)) is None:
                continue
            metrics[k] = {"value": val(m.get(k))}
            if not v.get("isControl") and up(m.get(k)) is not None:
                metrics[k]["uplift"] = up(m.get(k))
        if val(n.get("pct_subscription_orders")) is not None:
            metrics["new_customer_pct_subscription_orders"] = {
                "value": val(n["pct_subscription_orders"])}
            if not v.get("isControl") and up(n["pct_subscription_orders"]) is not None:
                metrics["new_customer_pct_subscription_orders"]["uplift"] = up(n["pct_subscription_orders"])
        rows.append({"name": v.get("name"), "is_control": bool(v.get("isControl")),
                     "visitors": val(m.get("n_visitors")), "orders": val(m.get("n_orders")),
                     "metrics": metrics, "_raw": m, "_pct": v.get("percentage")})

    notes = checks(item, rows, tr, overview, hero_metric, why, days)
    for r in rows:
        r.pop("_raw"), r.pop("_pct")
    return {
        "id": tid, "name": item.get("name"), "type": item.get("type"),
        "started_at": started.date().isoformat() if started else None,
        "days_live": days,
        "traffic_split": "/".join(str(v.get("percentage")) for v in variations),
        "analytics_view": (item.get("experienceAnalysis") or {}).get("analyticsViewType"),
        "hero_metric": hero_metric, "readiness": readiness, "verdict": verdict,
        "note": " ".join(notes), "variations": rows,
        "_redirects": [rd for v in variations for rd in (v.get("redirects") or [])
                       if rd.get("destinationUrl") and not rd.get("skip")],
    }


# ---------------------------------------------------------------------------
# Data checks -> note text. Each states what it found; none upgrades a badge.
# ---------------------------------------------------------------------------
def checks(item, rows, tr, overview, hero_metric, hero_why, days):
    notes = []
    ctrl = next((r for r in rows if r["is_control"]), None)
    chal = [r for r in rows if not r["is_control"]]
    orders = [r["orders"] or 0 for r in rows]

    verdict = tr.get("verdict")
    if not verdict:
        notes.append("Intelligems returned no verdict, so readiness falls back to Needs time.")
    elif verdict == "error":
        notes.append(f"Intelligems couldn't compute a verdict ({tr.get('reason') or 'no reason given'}), "
                     "so readiness falls back to Needs time.")
    elif verdict not in READINESS:
        notes.append(f"Unrecognised verdict '{verdict}', so readiness falls back to Needs time.")

    notes.append(" / ".join(f"{o:,.0f}" for o in orders) + " orders.")

    if hero_metric.get("assumed"):
        notes.append(f"Primary metric not verified ({hero_why}); RPV shown instead.")

    if any(o < TINY_ORDERS for o in orders):
        notes.append(f"Under {TINY_ORDERS} orders in an arm: the lifts below are noise, not results.")

    total_vis = sum(r["visitors"] or 0 for r in rows)
    if total_vis >= IMBALANCE_MIN_VISITORS:
        for r in rows:
            if not r["_pct"]:
                continue
            exp = total_vis * r["_pct"] / 100
            if exp and abs((r["visitors"] or 0) - exp) / exp > IMBALANCE_REL:
                share = ", ".join(f"{(x['visitors'] or 0) / total_vis * 100:.0f}%" for x in rows)
                split = "/".join(str(x["_pct"]) for x in rows)
                notes.append(f"Traffic imbalance: visitors split {share} against a configured {split}. "
                             "That points to an assignment or tracking fault, not a result.")
                break

    if re.search(r"\bA\s*/\s*A\b", item.get("name") or "", re.I):
        hits = []
        for r in chal:
            for k in FUNNEL_FOR_AA:
                m = r["_raw"].get(k)
                p = m.get("p2bc") if isinstance(m, dict) else None
                if p is not None and (p >= AA_SIG or p <= 1 - AA_SIG):
                    hits.append(f"{k} {pct(up(m) or 0)} (p2bc {p:.3f})")
        if hits:
            notes.append("A/A TEST SHOWING DIFFERENCES: " + "; ".join(hits) + ". Both arms are "
                         "identical, so the measurement setup is suspect, and that undermines "
                         "every other test on this account.")

    if ctrl and chal:
        hk = hero_metric["key"]
        lead = max(chal, key=lambda r: (r["metrics"].get(hk) or {}).get("value") or float("-inf"))
        cvr = (lead["metrics"].get("conversion_rate") or {}).get("uplift")
        aov = (lead["metrics"].get("net_revenue_per_order") or {}).get("uplift")
        rpv = (lead["metrics"].get("net_revenue_per_visitor") or {}).get("uplift")
        if (cvr is not None and aov is not None and abs(cvr) >= SIGN_CONFLICT_MIN
                and abs(aov) >= SIGN_CONFLICT_MIN and (cvr > 0) != (aov > 0)):
            driver = "CVR" if abs(cvr) > abs(aov) else "AOV"
            rp = f", net RPV {pct(rpv)}" if rpv is not None else ""
            notes.append(f"{lead['name']}: CVR {pct(cvr)} and AOV {pct(aov)} pull in opposite "
                         f"directions{rp}; {driver} is the larger move.")
        hero_m = lead["_raw"].get(hk) if hk in lead["_raw"] else None
        if isinstance(hero_m, dict) and up(hero_m) is not None:
            notes.append(f"{lead['name']} {hero_metric['label']} {pct(up(hero_m))} "
                         f"({chance(hero_m.get('p2bc'))} probability to beat control).")

    opv = tr.get("orders_per_variant") or {}
    if opv and sum(orders):
        tot = sum(opv.values())
        if abs(tot - sum(orders)) / sum(orders) > 0.05:
            notes.append(f"The verdict counts {tot:,} orders against this table's {sum(orders):,.0f}, "
                         "so it is computed on a narrower slice than the rows below.")
    rd = tr.get("runtime_days")
    if rd is not None and abs(rd - days) > 2:
        notes.append(f"Intelligems reports {rd} days of runtime against {days} since the latest start; "
                     "days live counts from the start.")
    if tr.get("conflicting_metrics"):
        notes.append("Conflicting metrics: " + ", ".join(
            f"{c.get('key')} {pct(c.get('uplift') or 0)}" for c in tr["conflicting_metrics"]) + ".")

    imp = (overview.get("impact") or {}).get("est_monthly_revenue_increase")
    if imp is not None and abs(imp) >= IMPACT_MENTION:
        notes.append(f"Intelligems estimates {money(imp)}/month.")
    return notes


# ---------------------------------------------------------------------------
# Traffic sheet
# ---------------------------------------------------------------------------
def traffic_block(tests, fetched):
    g_rows = list(csv.DictReader(io.StringIO(fetch_text(SHEET_GROUPS))))
    l_rows = list(csv.DictReader(io.StringIO(fetch_text(SHEET_LPS))))
    have_lp_col = bool(l_rows) and traffic_breakdown.LP_COL in l_rows[0]
    base = traffic_breakdown.build(g_rows, [], [], [])
    sheet_groups = {traffic_breakdown_norm(k): k for k in base["groups"]}

    # A redirect test's challenger page is filed under the page it lands on, i.e.
    # outside the test's group. Move it back (see SKILL.md 5c), when we can.
    reassign, failed = [], {}
    for t in tests:
        tags = t.get("groups") or re.findall(r"\[([^\[\]]+)\]", t["name"] or "")
        target = next((sheet_groups[traffic_breakdown_norm(g)] for g in tags
                       if traffic_breakdown_norm(g) in sheet_groups), None)
        for rd in t["_redirects"]:
            p = path_of(rd["destinationUrl"])
            if not target or not p or not path_of(rd.get("originUrl")):
                continue
            if not have_lp_col:
                failed.setdefault(t["id"], []).append(p)
                continue
            try:
                traffic_breakdown.build(g_rows, l_rows, [], [(p, target)])
                reassign.append((p, target))
            except SystemExit:
                failed.setdefault(t["id"], []).append(p)

    res = traffic_breakdown.build(g_rows, l_rows if have_lp_col else [],
                                  ["Other"] if have_lp_col else [], reassign)
    breakdowns = {g: b for g, b in res["breakdowns"].items()
                  if any(not r.get("rest") for r in b["rows"])}
    for t in tests:
        if t["id"] in failed:
            t["note"] += (" Its redirect page" + ("s " if len(failed[t["id"]]) > 1 else " ")
                          + ", ".join(failed[t["id"]]) + " couldn't be moved into its group this refresh"
                          + (" (the sheet's full_lps tab has no Landing Page column)." if not have_lp_col
                             else " (not found in the sheet's full_lps tab)."))

    src = (f"Meta ads by landing-page group, published sheet (groups tab, {len(g_rows)} rows) as "
           f"fetched {fetched}, covering the previous day")
    if "Other" not in breakdowns:
        src += ". Other isn't broken down this refresh" + (
            ": the sheet's full_lps tab has no Landing Page column" if not have_lp_col else "")
    return {"source": src, "denominator_label": "paid LP sessions",
            "total_sessions": sum(res["groups"].values()), "window_days": SHEET_WINDOW_DAYS,
            "groups": res["groups"], "group_orders": res["group_orders"],
            "breakdowns": breakdowns, "reassigned": res["reassigned"]}


def traffic_breakdown_norm(name):
    return " ".join(str(name).split()).strip().upper()


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--out", required=True, help="pulled JSON to write")
    ap.add_argument("--overrides", default=str(Path(__file__).resolve().parents[4] / "overrides.json"),
                    help="per-test groups/note overrides (default: repo-root overrides.json)")
    ap.add_argument("--cache-dir", help="also save every raw API response here, for debugging")
    ap.add_argument("--no-traffic", action="store_true", help="skip the Google Sheet")
    a = ap.parse_args()

    key = os.environ.get("INTELLIGEMS_API_KEY")
    if not key:
        raise SystemExit("INTELLIGEMS_API_KEY is not set")
    api = Api(key, a.cache_dir)
    overrides = json.loads(Path(a.overrides).read_text()) if Path(a.overrides).exists() else {}
    overrides = {k: v for k, v in overrides.items() if not k.startswith("_")}

    now = datetime.now(timezone.utc)
    live = list_live(api)
    print(f"{len(live)} live experiments", file=sys.stderr)

    # Pull everything first so the whole board reflects one moment.
    raw = []
    for it in live:
        tid = it["id"]
        print(f"  {it.get('name')}", file=sys.stderr)
        detail = api.call("GET", f"/experiences/{tid}", tag=f"{tid}-detail")
        ov = api.call("POST", f"/analytics/resource/{tid}",
                      body={"view": "overview", "testResult": True}, tag=f"{tid}-overview")
        nc = api.call("POST", f"/analytics/resource/{tid}",
                      body={"view": "overview", "filters": {"customerType": "new"}}, tag=f"{tid}-newcust")
        raw.append((it, detail, ov, nc))

    tests = []
    for it, detail, ov, nc in raw:
        t = build_test(it, detail, ov, nc, now)
        o = overrides.get(t["id"]) or {}
        if o.get("groups"):
            t["groups"] = o["groups"]
        if o.get("note"):
            t["note"] = (t["note"] + " " + o["note"]).strip()
        tests.append(t)

    stamp = now.astimezone().isoformat(timespec="minutes")
    out = {"generated_at": stamp, "organization": "First Day Life Inc.", "tests": tests}
    if not a.no_traffic:
        out["traffic"] = traffic_block(tests, now.date().isoformat())
    for t in tests:
        t.pop("_redirects", None)
    Path(a.out).write_text(json.dumps(out, indent=1))
    print(f"wrote {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
