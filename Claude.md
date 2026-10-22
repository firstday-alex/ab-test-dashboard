# CLAUDE.md — ab-test-dasboard

Published copy of First Day's live A/B test dashboard. `index.html` is a generated artifact served
by GitHub Pages; it is not hand-edited.

## What this repo is

A single self-contained HTML file listing every Intelligems A/B test currently running. Each test is
one collapsible row: name, days live, variant orders, lift on the hero metric, and a readiness badge.
Expanding a row reveals CVR, RPV, AOV, ATC, cart abandonment, and checkout completion per variation
with lift vs. control.

Everything in `index.html` is regenerated from a fresh Intelligems pull. There is no incremental
update path and no state carried between runs — a "refresh" is the whole pipeline run again.

## Repo layout

```
index.html                       generated dashboard, served by Pages — do not hand-edit
.claude/skills/intelligems-live-test-dashboard/
    SKILL.md                     the refresh workflow
    scripts/build_dashboard.py   renders index.html from a pulled JSON file
```

The build script lives under the skill directory — there is no top-level `scripts/`.

Layout and formatting live in `build_dashboard.py`. If the dashboard needs to look different, change
the script and regenerate. Editing `index.html` directly means the next refresh silently reverts it.

## Regenerating

The `intelligems-live-test-dashboard` skill owns this workflow. In short:

1. `list_organizations` → First Day Life Inc. is `86ab4ded-43f9-4a0a-a9ef-80dfd2b5508d`. Pass it as
   `organization` on every call.
2. `search_experiments(status="started")` → the live A/B tests. Experiments only; `search_experiences`
   returns evergreen programs, which this dashboard deliberately excludes.
3. Per test, three calls:
   - `analyze_experience(experienceId=…, view="overview", testResult=true)` → every metric below per
     variation, plus visitors, orders, variation names, `isControl`, and split percentages.
   - `analyze_experience(experienceId=…, view="overview", filters={"customerType": "new"})` → only
     for `pct_subscription_orders`, stored as `new_customer_pct_subscription_orders`.
   - `get_experience(id=…)` → only for the primary key metric.
4. Write the pulled JSON, then
   `python3 .claude/skills/intelligems-live-test-dashboard/scripts/build_dashboard.py <pulled.json> -o index.html`.

Pull everything before rendering so the whole dashboard reflects one moment in time.

### Metric keys

| Column          | Intelligems key                       |
|-----------------|---------------------------------------|
| CVR             | `conversion_rate`                     |
| RPV             | `net_revenue_per_visitor`             |
| AOV             | `net_revenue_per_order`               |
| ATC             | `add_to_cart_rate`                    |
| Cart aband.     | `abandoned_cart_rate`                 |
| Checkout compl. | `abandoned_checkout_rate` — see below  |
| New-cust. sub % | `pct_subscription_orders` from the `customerType: "new"` pull |
| Visitors        | `n_visitors`                          |
| Orders          | `n_orders`                            |

Checkout completion is **not** an API metric. Store `abandoned_checkout_rate` as pulled; the build
script renders the completion column as `1 - rate` via its `COMPLEMENT_OF` registry, and recomputes
the lift from the two completion values — the lift of a complement is not the negated lift of the
source. Abandonment metrics carry `higher_is_better=False` in `METRICS`, which flips the lift colour
only; the printed sign always follows the data.

Take `value` and `uplift.value`. The dashboard is deliberately lift-only — the confidence intervals
and p2bb/p2bc that come back on every metric are for interpretation in chat, not for these rows.

## API gotchas — all of these were hit in practice

**`testResult.runtime_days` is unreliable. Do not use it.** It appears to count from creation rather
than start: a test created nine days before launch and live for one day reports 10. Compute days live
from `startedAtTs` (available on `search_experiments`) instead. This is the only figure that matches
what the test has actually been collecting.

**The primary metric is not always RPV.** It varies per test — `[Broad KCM PDP] Price per Gummy` is
primaried on ATC while the test beside it uses RPV. Read the `experienceKeyMetrics` entry with
`isPrimary: true` and map its `standardEventId` (`conversionRate`, `netRevenuePerVisitor`,
`netRevenuePerOrder`, `addToCartRate`, `profitPerVisitor`). If it genuinely can't be verified, set
`"assumed": true` on that test's `hero_metric` so the label renders with an asterisk — never pass a
guess off as verified.

**`get_variation_overview` does not expose `add_to_cart_rate`.** That's why `analyze_experience` is
the call to make. Don't split the pull across both tools.

**`get_experience` responses can be enormous.** Variations carrying onsite edits return the injected
HTML/CSS/JS inline. Pull it once per test and extract only the primary-metric flag.

**Parameter naming is inconsistent.** Most tools take the UUID as `id`; `analyze_experience` and
`generate_intelligems_graph` take it as `experienceId`. On a validation error, retry with the other.

## Readiness badge

Derived from `testResult.verdict`, never from judgment:

| Verdict                                                | Badge                 |
|--------------------------------------------------------|-----------------------|
| `not_ready`                                            | Needs time            |
| `directional_win`, `directional_loss`, `mixed_signals` | Could make a decision |
| `strong_win`, `strong_loss`                            | Time to make a call   |

`error` or absent → Needs time, with the reason in the test's `note`. Never upgrade a badge because a
lift looks big. The badge exists specifically to stop a decision being made on a three-day read.

## Check the data before publishing

Live-test data breaks in ways the verdict field won't tell you. Put anything found in the test's
`note`; a row that renders a bogus lift without comment is worse than one that says so.

- **Traffic imbalance** — compare visitors per variation against the configured split. A 50/50 test
  showing 10:1 is an assignment or tracking failure, not a result.
- **A/A tests showing significance** — an A/A should show nothing. If one shows a near-certain lift
  on any funnel step, the measurement setup itself is suspect, and that undermines every other test
  on the account. Surface it prominently.
- **Tiny denominators** — a handful of orders produces lifts in the hundreds of percent. Report them
  and say plainly they aren't interpretable.
- **Sign conflicts** — CVR up with AOV down nets out in RPV. Name which side is driving it rather
  than letting the hero lift stand alone.

## Hard rules

- **Every number comes from an Intelligems call in the current session.** No recalled, estimated, or
  illustrative figures. A missing metric stays null and renders as an em dash.
- **Read-only against Intelligems.** Never call `experience_action`, `create_experience`,
  `update_experience`, or any price-test mutation. Building a dashboard never requires changing a test.
- **Auto-push refreshes.** Commit `index.html` and push to `main` as the last step of a refresh, without
  asking. Standing authorization from the repo owner — no per-run approval needed. Still report the
  commit SHA. If a push fails on auth, report it and stop: don't retry against a different remote,
  rewrite history, or commit elsewhere to appear successful.
- **Auto-push does not mean publish silently.** A refresh can only be pushed after the visual check
  below passes and the data checks above have been run, with anything they turn up written into the
  affected test's `note`. Pushing a row whose lift is known to be bogus, uncommented, is the one thing
  this pipeline must never do — the dashboard is what the team reads instead of Intelligems.
- **Don't hand-write the HTML.** Change the script.

## UI changes must be verified visually

The script's layout has broken before in a way that passed every text-based check — CSS grid
auto-placement inside a media query put two stat blocks in the same cell, and string assertions
against the HTML all passed while the render was visibly garbled.

After any layout change, render at multiple widths and check for collisions programmatically:

```python
boxes = pg.eval_on_selector_all(
    "summary .stat, summary .t-name, summary .pill",
    "els => els.map(e => { const r = e.getBoundingClientRect();"
    "return {t:r.top, b:r.bottom, l:r.left, rt:r.right} })")
```

Then test every pair for rectangle intersection. Run it at 380px, 760px, and 1180px, and look at the
screenshots too. Flexbox with `flex-wrap` is used deliberately here because it cannot overlap by
construction — don't reintroduce grid auto-placement for the summary row.

## Publishing

`index.html` at the repo root is what Pages serves. Requires git credentials, so it won't work from a
sandboxed chat session (no token, no `gh`, empty `~/.ssh`) — only from a local session or CI.

```bash
cp <generated>.html index.html
git add index.html
git commit -m "Refresh live test dashboard $(date -u +%Y-%m-%d)"
git push origin main
```

For automatic refreshes, a scheduled GitHub Action that runs the pull and build in-repo is the better
shape — it needs an Intelligems API credential as a repo secret.

## Notes

- Repo is `firstday-alex/ab-test-dashboard` — spelled correctly, with the `h`. Earlier notes here (and
  the skill's `SKILL.md`) claimed `ab-test-dasboard`; that was wrong, verified against `git remote -v`
  on 2026-08-20. Use `origin` rather than a hardcoded URL.
- GitHub Pages is enabled, serving `main` at root: https://firstday-alex.github.io/ab-test-dashboard/
  A push triggers a rebuild; the live page lags the commit by a minute or two. Confirm a refresh
  actually went live by checking the `data pulled` stamp on the served page, not just the push result.
- API behaviors above were verified against the live account on 2026-08-19, re-confirmed 2026-08-20.
  Re-check `runtime_days` and the key-metric shape if Intelligems ships a new API version.