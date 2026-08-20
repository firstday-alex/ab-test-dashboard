---
name: intelligems-live-test-dashboard
description: >
  Pull every currently-live A/B test from the Intelligems MCP and build (or refresh) a collapsible
  HTML dashboard where each test is one row that expands to reveal CVR, RPV, AOV, and ATC per
  variation. Use this skill whenever the user asks to "refresh the test dashboard", "what tests are
  live", "show me all our active tests", "how are our running tests doing", "update the dashboard",
  "live test dashboard", "test tracker dashboard", or wants a single view of all in-flight
  experiments rather than a readout of one specific test. Also trigger on vaguer asks like "where do
  our tests stand" or "anything ready to call?" — those are this dashboard, not a single-test
  readout. Always pull live data from the Intelligems MCP; never recall or estimate test numbers.
---

# Intelligems Live Test Dashboard

Produce one self-contained HTML file listing every A/B test currently running, collapsed to a
scannable row per test, expanding to a per-variation metrics table.

For a deep readout of a **single** test (hypothesis, funnel decomposition, slides), use the
`intelligems-ab-test-readout` skill instead. This skill is the fleet-level view.

## Hard rules

- **Every number must come from an Intelligems MCP call made in this session.** No recalled,
  estimated, interpolated, or illustrative figures. If a metric comes back missing, leave it null
  in the JSON — the build script renders an em dash, which is the honest answer.
- **Read-only.** Never call `experience_action`, `create_experience`, `update_experience`, or any
  price-test mutation. Building a dashboard never requires changing anything.
- **Don't hand-write the HTML.** Pull data into JSON, then run `scripts/build_dashboard.py`. The
  script owns layout, sorting, and formatting so every refresh looks identical and diffs cleanly.
- Data moves while tests are live. Stamp the pull time and pull everything before rendering.

## Workflow

### 1. Resolve the organization

Call `list_organizations` once. Pass its `id` as `organization` on every later call. If the user has
multiple stores and hasn't said which, ask before pulling.

### 2. Find the live tests

```
search_experiments(status="started", limit=100)
```

Experiments are A/B tests; experiences are evergreen non-test programs. This dashboard covers
experiments only — do not fold in `search_experiences` results unless the user explicitly asks.
Paginate with `page` if the result set is capped. If nothing is live, still build the dashboard: the
script renders an explicit empty state, which is more useful than a chat message.

### 3. Per test, make two calls

```
get_experience(id=<uuid>)
analyze_experience(experienceId=<uuid>, view="overview", testResult=true)
```

**Parameter quirk:** most tools take the UUID as `id`, but `analyze_experience` and
`generate_intelligems_graph` take it as `experienceId`. If a call fails validation, retry with the
other name.

`get_experience` is needed for exactly one thing: the configured key metrics and **which is
primary**. `analyze_experience` already returns variation names, `isControl`, and split percentages,
so don't re-derive those. Note `get_experience` responses can be very large when a variation carries
onsite edits (injected HTML/CSS/JS is returned inline), so pull it once per test and extract only
what you need.

`analyze_experience` with `view="overview"` returns per variation, in one shot, all four metrics
this dashboard needs plus visitors and orders. Each statistical metric arrives as an object with
`value`, `ci_low`, `ci_high`, `plus_minus`, `p2bb`, `p2bc`, and `uplift` — take `value` and
`uplift.value` and ignore the rest; this dashboard is deliberately lift-only.

Metric keys to read:

| Dashboard column | Intelligems key             |
|------------------|-----------------------------|
| CVR              | `conversion_rate`           |
| RPV              | `net_revenue_per_visitor`   |
| AOV              | `net_revenue_per_order`     |
| ATC              | `add_to_cart_rate`          |
| Visitors         | `n_visitors`                |
| Orders           | `n_orders`                  |

Note that `get_variation_overview` does **not** expose `add_to_cart_rate` — that's why
`analyze_experience` is the call to make here. Don't split the pull across both tools.

Run these calls for all tests before writing any JSON, so the whole dashboard reflects one moment.

### 4. Derive the row fields

- **days_live** — compute as whole days from `startedAtTs` (from `search_experiments`) to now.
  **Do not use `testResult.runtime_days`.** It is unreliable: it appears to count from creation
  rather than start, so a test created 9 days before launch and live for 1 day reports 10. It also
  disagrees with actual runtime on tests that were never paused. Computing from `startedAtTs` is the
  only figure that matches what the test has actually been collecting.
- **hero_metric** — the primary key metric from `get_experience`: the entry in
  `experienceKeyMetrics` with `isPrimary: true`. Map its `standardEventId` to a metric key
  (`conversionRate`→`conversion_rate`, `netRevenuePerVisitor`→`net_revenue_per_visitor`,
  `netRevenuePerOrder`→`net_revenue_per_order`, `addToCartRate`→`add_to_cart_rate`,
  `profitPerVisitor`→`gross_profit_per_visitor`).

  **Do not assume the primary metric is RPV.** Real accounts vary it per test — a PDP price-display
  test may be primaried on ATC while the test next to it uses RPV. If you genuinely can't verify it
  for a test, set `"assumed": true` on that test's `hero_metric`; the script marks the label with an
  asterisk and spells out "assumed — primary not verified" in the expanded meta line. Flag it in the
  chat summary too. An unlabelled guess here silently mislabels which number the team should be
  reading.
- **readiness** — map the `testResult.verdict` returned by `analyze_experience`:

  | Verdict                                        | readiness      | Badge                 |
  |------------------------------------------------|----------------|-----------------------|
  | `not_ready`                                    | `needs_time`   | Needs time            |
  | `directional_win`, `directional_loss`, `mixed_signals` | `could_decide` | Could make a decision |
  | `strong_win`, `strong_loss`                    | `call_it`      | Time to make a call   |

  If the verdict is `error` or absent, fall back to `needs_time` and put the reason in the test's
  `note` field. Never upgrade a test's readiness on a hunch — the badge is a data-derived signal,
  and inflating it is how someone ships a change on noise.

The script picks the leading challenger (highest hero-metric value) for the collapsed row's variant
orders and hero-metric lift, and shows all variations once expanded — you don't need to pre-select.

### 5. Write the JSON

Save to `/home/claude/live_tests.json`. Uplift values are fractions as returned by the API
(`0.055` = +5.5%), not percentages. Omit or null any metric the API didn't return.

```json
{
  "generated_at": "2026-08-19T09:15-05:00",
  "organization": "Store name",
  "tests": [
    {
      "id": "uuid",
      "name": "PDP sticky ATC bar",
      "type": "content",
      "started_at": "2026-08-01",
      "days_live": 18,
      "traffic_split": "50/50",
      "analytics_view": "sitewide_orders",
      "hero_metric": { "key": "net_revenue_per_visitor", "label": "RPV" },
      "readiness": "could_decide",
      "verdict": "directional_win",
      "note": "optional one-line caveat, e.g. why readiness fell back",
      "variations": [
        {
          "name": "Control", "is_control": true, "visitors": 41230, "orders": 902,
          "metrics": {
            "conversion_rate": { "value": 0.0219 },
            "net_revenue_per_visitor": { "value": 1.42 },
            "net_revenue_per_order": { "value": 64.8 },
            "add_to_cart_rate": { "value": 0.0871 }
          }
        },
        {
          "name": "V1: sticky bar", "is_control": false, "visitors": 41102, "orders": 948,
          "metrics": {
            "conversion_rate": { "value": 0.0231, "uplift": 0.0548 },
            "net_revenue_per_visitor": { "value": 1.51, "uplift": 0.0634 },
            "net_revenue_per_order": { "value": 65.4, "uplift": 0.0093 },
            "add_to_cart_rate": { "value": 0.0902, "uplift": 0.0356 }
          }
        }
      ]
    }
  ]
}
```

Add `"assumed": true` inside `hero_metric` when the primary metric wasn't verified from config.

The script hard-fails on a missing `id`, `name`, `days_live`, `readiness`, or `variations`, and on an
unrecognized readiness value. That's intentional — a row with a guessed badge is worse than no row.

### 5b. Sanity-check before rendering

Live-test data is often broken in ways the verdict field won't tell you. Check each test and put
anything you find in its `note` — a dashboard that renders a bogus lift without comment is worse
than one that says the number can't be trusted:

- **Traffic imbalance.** Compare visitors per variation against the configured split. A 50/50 test
  showing 10:1 visitors is an assignment or tracking failure, not a result.
- **Funnel-rate impossibilities.** Wildly different `view_product_page_rate` between groups on a
  test that doesn't touch traffic sources points the same direction.
- **A/A tests showing significance.** An A/A should show no difference. If one shows a near-certain
  lift on any funnel step, that's evidence the measurement setup itself is suspect — and it
  undermines the other tests on the same account, so surface it prominently.
- **Uplifts from tiny denominators.** A few orders per group produce lifts in the hundreds of
  percent. Report them, but say plainly that they're not interpretable.
- **Sign conflicts.** CVR up with AOV down (or vice versa) nets out in RPV; name which side is
  driving it rather than letting the hero lift stand alone.

`impact.est_monthly_revenue_increase` is worth including in the note when it's large in either
direction, labelled as Intelligems' estimate.

### 5c. Traffic groups

Each test name carries the landing-page groups it runs on in square brackets, and a test may span
several: `[TDK-BTS][KDE-BTS] BTS Multi Product Tab Placement` covers both. The script parses these
from the name; pass an explicit `"groups": [...]` on a test only to override.

Sessions per group come from a published Google Sheet CSV (Meta ads bucketed by landing-page group):

```bash
curl -sL "https://docs.google.com/spreadsheets/d/e/2PACX-1vR-UNFL2QOO463D9C72xV0GZX5BD_WYTPPzXInUkZ2cuiuS0n6--G-rKKz7IZ2eZCsWwETiVQ9PJTFd/pub?output=csv" -o groups.csv
```

Columns: `Ad Name, Landing Page Path Groups, Sessions, ATC Rate, Bounce Rate, Avg Engaged Time (mins), CVR, Orders`.
Sum `Sessions` per group and add a top-level `traffic` block:

```json
"traffic": {
  "source": "Meta ads by landing-page group, 2026-08-19 (1 day)",
  "denominator_label": "paid LP sessions",
  "total_sessions": 53517,
  "groups": { "Broad PDP": 23397, "Other": 15076, "TDK-BTS": 10236, "KDE-BTS": 4390, "KCM-Melatonin": 418 }
}
```

**Per test, traffic is the sum of that test's own groups. Across tests it is the UNION, never the
sum.** Two tests on `Broad PDP` cover those 23,397 sessions once, not twice — summing produced 114.8%
coverage in testing, which is impossible on its face. The script owns this arithmetic in `coverage()`;
don't recompute it by hand.

Matching is case- and whitespace-insensitive. A group tag with no row in the sheet renders an em dash
and is called out in the expanded meta line as unmatched — **never substitute a similarly-named group.**
`[KCM-Sales]` and the sheet's `KCM-Melatonin` are probably the same surface, but "probably" is not a
number, so it stays unjoined until the names actually agree.

**The sheet is the only source for traffic share. Do not bring Intelligems visitor counts into this
calculation.** `total_sessions` is the sum of `Sessions` across the sheet's rows, and every percentage
on the dashboard — per-test, roll-up, and opportunities — is denominated on that figure alone. The
sheet defines the traffic universe for this purpose; Intelligems supplies test metrics only.

**Sitewide groups are excluded from the roll-up.** List them in `traffic.sitewide_groups` (e.g.
`["Cart"]`); they resolve to the full denominator, so the row reads 100% and its stat label reads
`sitewide` instead of `traffic`. Anything at 100% is then dropped from the coverage numerator and from
the opportunities list by `is_sitewide()`. A cart drawer is on every session, so it says nothing about
which landing-page surfaces are under test — counting it would peg coverage at 100% and hide exactly
the gaps the roll-up exists to show. The rule keys off the value, not the name, so a sheet group that
ever reaches 100% is handled the same way.

A test combining a sitewide group with a surface group (`[Cart][TDK-BTS]`) reads 100% on its row —
sitewide subsumes the rest rather than summing past the denominator — while `TDK-BTS` still counts
toward coverage on its own.

### 6. Build and deliver

```bash
python3 scripts/build_dashboard.py /home/claude/live_tests.json \
  -o /mnt/user-data/outputs/intelligems-live-tests.html
```

Always write to the same filename, `intelligems-live-tests.html`. "Refresh" means regenerate the
whole file from a fresh pull — there's no incremental update path and no state carried between runs,
so a refresh is just this workflow run again. Present the file, then give a 2–4 line chat summary:
how many tests are live, which are in `call_it`, and anything odd (a test with almost no traffic, a
missing metric, an ambiguous hero metric).

Keep the chat summary short. The dashboard is the deliverable; don't restate every test's numbers in
prose.

### 7. Publish

Auto-push is standing authorization from the repo owner — no per-run approval. Only push after the
visual check passes and the data checks in 5b have run, with findings written into the affected
test's `note`.

```bash
cp <generated>.html index.html
git add index.html
git commit -m "Refresh live test dashboard $(date -u +%Y-%m-%d)"
git push origin main
```

Use `origin`, not a hardcoded URL. Report the SHA. Pages serves `main` at root and lags the push by
a minute or two — confirm the refresh actually went live by checking the `data pulled` stamp on the
served page, not the push result. If a push fails on auth, report it and stop: don't retry against a
different remote, rewrite history, or commit elsewhere to appear successful.

Publishing needs real git credentials, so it only works from a local session or CI, not a sandboxed
chat session. To take the local session out of the loop entirely, a scheduled GitHub Action that runs
the pull and build in-repo is the better shape — that needs an Intelligems API credential as a repo
secret.

## Output shape

Collapsed row, left to right: test name · days live · variant orders · hero-metric lift · readiness
badge. Rows sort by readiness (call it → could decide → needs time), then by days live descending,
so the tests wanting attention sit at the top.

Expanded: metadata line (start date, split, type, hero metric, analytics view, raw verdict), then a
table with one row per variation — Visitors, Orders, CVR, RPV, AOV, ATC — control first, with values
plus lift vs. control on each challenger, and a link back to the test in Intelligems.

## Variations on the request

- **"Just tell me / no file"** — pull the same data and answer in a compact markdown table. Don't
  build the HTML if the user doesn't want a file.
- **"Add a metric"** (profit/visitor, units per order, subscription share) — `analyze_experience`
  returns many more metrics than these four. Add the key to the `METRICS` list at the top of
  `build_dashboard.py` with a format of `pct`, `money`, or `int`, and register a label in
  `METRIC_LABELS`. Prefer editing the script over hand-writing HTML.
- **"Segment by device / new vs returning"** — re-pull with `view="audience"` and
  `audience="device_type"`, and say clearly in the summary that the dashboard is filtered.
- **"Include paused tests"** — run `search_experiments(status="paused")` too and add the status to
  each test's `note` so a paused test is never mistaken for a running one.
- **"Include ended tests"** — that's a different job; point at `intelligems-ab-test-readout` or ask
  whether they want a historical archive view.

## Reading the numbers honestly

- A `needs_time` badge on a test with a big lift is the common trap. Early lifts are mostly noise;
  the badge exists precisely to stop a decision being made on a 3-day read.
- CVR lift and AOV lift multiply into RPV lift. If RPV is up but CVR is flat, the gain is coming
  from basket value, not from more buyers — worth flagging in the summary.
- ATC up with CVR flat means the change moved intent but something downstream ate it. That's a
  pointer to run the full readout skill on that test, and worth calling out.
- A challenger winning on the hero metric while losing on the others isn't automatically a winner.
  When the picture is mixed, say so rather than letting the single hero lift imply a verdict.
