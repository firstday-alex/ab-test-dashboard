# ab-test-dashboard

Published copy of First Day's live A/B test dashboard — every Intelligems experiment currently
running, one collapsible row each. Expanding a row shows CVR, RPV, AOV, ATC, cart abandonment, and
checkout completion per variation, with lift vs. control.

**Live:** https://firstday-alex.github.io/ab-test-dashboard/

`index.html` at the repo root is what GitHub Pages serves. It is a **generated artifact** — never
hand-edit it. The next refresh regenerates the whole file and silently reverts anything you typed
into it. If the dashboard needs to look different, change the build script.

---

## How to refresh it

A refresh is the entire pipeline run again. There is no incremental update path and no state carried
between runs.

### Prerequisites

- **A local Claude Code session** in this repo. Publishing needs real git credentials, so a sandboxed
  chat session can't finish the job (no token, no `gh`, empty `~/.ssh`).
- **The Intelligems MCP server connected**, authenticated against First Day Life Inc.
- **Python 3** for the build script. No third-party packages needed to build.
- **Playwright** only if you changed the layout — see [Visual check](#visual-check-only-after-a-layout-change).

### The short version

Just ask, in a local session:

> refresh the test dashboard

That triggers the `intelligems-live-test-dashboard` skill in
`.claude/skills/intelligems-live-test-dashboard/`, which owns the whole workflow — pull, sanity
checks, build, visual check, commit, push. The skill has standing authorization to push, so you get
a commit SHA back rather than an approval prompt.

### The long version, if you're doing it by hand

1. **Resolve the org.** `list_organizations` → First Day Life Inc. is
   `86ab4ded-43f9-4a0a-a9ef-80dfd2b5508d`. Pass it as `organization` on every subsequent call.

2. **Find the live tests.** `search_experiments(status="started", limit=100)`.
   Experiments only — `search_experiences` returns evergreen programs, which this dashboard
   deliberately excludes.

3. **Two calls per test.** Pull everything before rendering, so the whole board reflects one moment.
   - `analyze_experience(experienceId=…, view="overview", testResult=true)` — every metric the
     dashboard shows, per variation, plus visitors, orders, variation names, `isControl`, split
     percentages, and the verdict.
   - `get_experience(id=…)` — needed for exactly one thing: which key metric is primary.

4. **Pull the traffic sheet.** Sessions per landing-page group come from a published Google Sheet
   (Meta ads bucketed by landing-page group):

   ```bash
   curl -sL "https://docs.google.com/spreadsheets/d/e/2PACX-1vR-UNFL2QOO463D9C72xV0GZX5BD_WYTPPzXInUkZ2cuiuS0n6--G-rKKz7IZ2eZCsWwETiVQ9PJTFd/pub?output=csv" -o groups.csv
   ```

   Sum `Sessions` per `Landing Page Path Groups`. Parse by header name, not column position — the
   column order has changed before.

5. **Write the pulled JSON**, then build:

   ```bash
   python3 .claude/skills/intelligems-live-test-dashboard/scripts/build_dashboard.py \
     live_tests.json -o intelligems-live-tests.html
   ```

   The JSON shape is documented in the skill's `SKILL.md`. The script hard-fails on a missing `id`,
   `name`, `days_live`, `readiness`, or `variations`, and on an unrecognized readiness value — that's
   deliberate, a row with a guessed badge is worse than no row.

6. **Publish:**

   ```bash
   cp intelligems-live-tests.html index.html
   git add index.html
   git commit -m "Refresh live test dashboard $(date -u +%Y-%m-%d)"
   git push origin main
   ```

   Use `origin`, not a hardcoded URL. Pages rebuilds on push and lags by a minute or two — confirm
   the refresh actually went live by checking the `data pulled` stamp on the served page, not the
   push result. If the push fails on auth, stop and report it: don't retry against a different
   remote, rewrite history, or commit elsewhere to look successful.

---

## Where things live

```
index.html                                                   generated dashboard, served by Pages — do not hand-edit
.claude/skills/intelligems-live-test-dashboard/SKILL.md      the workflow
.claude/skills/intelligems-live-test-dashboard/scripts/
    build_dashboard.py                                       renders index.html from a pulled JSON file
```

Note the script lives **under the skill directory**, not in a top-level `scripts/`. There is no
top-level `scripts/` — earlier revisions of `CLAUDE.md` pointed at one; that was corrected
2026-08-27.

---

## Check the data before you publish

Live-test data breaks in ways the verdict field won't tell you. Whatever you find goes in the
affected test's `note` — a row that renders a bogus lift without comment is worse than one that says
so plainly. This is the one thing the pipeline must never do, because the dashboard is what the team
reads *instead of* Intelligems.

- **Traffic imbalance** — visitors per variation vs. the configured split. A 50/50 test showing 10:1
  is an assignment or tracking failure, not a result.
- **A/A tests showing significance** — an A/A should show nothing. If one shows a near-certain lift
  on any funnel step, the measurement setup itself is suspect, and that undermines every other test
  on the account. Surface it prominently.
- **Tiny denominators** — a handful of orders produces lifts in the hundreds of percent. Report them
  and say plainly they aren't interpretable.
- **Sign conflicts** — CVR up with AOV down nets out in RPV. Name which side is driving it rather
  than letting the hero lift stand alone.
- **Where the funnel leaked** — the two abandonment columns localise an ATC-up/CVR-flat result: cart
  abandonment up means they never reached checkout, checkout completion down means they reached it
  and bailed. Say which one moved. Conversely, a CVR win with both funnel columns flat is claiming
  conversions from nowhere — treat that as a measurement smell, not a win.

The readiness badge comes from `testResult.verdict`, never from judgment. Never upgrade a badge
because a lift looks big — the badge exists specifically to stop a decision being made on a
three-day read.

---

## Visual check (only after a layout change)

The layout has broken before in a way that passed every text-based check: CSS grid auto-placement
inside a media query put two stat blocks in the same cell, and string assertions against the HTML all
passed while the render was visibly garbled.

If you changed `build_dashboard.py`'s layout, render at 380px, 760px and 1180px — **collapsed and
expanded** — and test every pair of summary-row boxes for rectangle intersection:

```python
boxes = pg.eval_on_selector_all(
    "summary .stat, summary .t-name, summary .pill",
    "els => els.map(e => { const r = e.getBoundingClientRect();"
    "return {t:r.top, b:r.bottom, l:r.left, rt:r.right} })")
```

Also assert that the page body never scrolls sideways, that no `.t-scroll` container is wider than
the viewport, and that no `tbody` cell has a zero-size box. The detail table is eight columns wide and
is *supposed* to scroll horizontally inside `.t-scroll` on narrow screens — that's containment
working, not a bug. If you add a column, raise `table{min-width:…}` in the `max-width:560px` media
query.

Look at the screenshots too. Playwright isn't installed system-wide here; a throwaway venv is the
easy route:

```bash
python3 -m venv pwenv && ./pwenv/bin/pip install playwright && ./pwenv/bin/python -m playwright install chromium
```

Flexbox with `flex-wrap` is used deliberately in the summary row because it cannot overlap by
construction. Don't reintroduce grid auto-placement there.

---

## Hard rules

- **Every number comes from an Intelligems call in the current session.** No recalled, estimated, or
  illustrative figures. A missing metric stays null and renders as an em dash.
- **Read-only against Intelligems.** Never call `experience_action`, `create_experience`,
  `update_experience`, or any price-test mutation. Building a dashboard never requires changing a test.
- **Auto-push, but not silently.** A refresh can be pushed without per-run approval, but only after
  the data checks have run and the visual check passes.
- **Don't hand-write the HTML.** Change the script.

---

## API gotchas — all hit in practice

**`testResult.runtime_days` is unreliable.** It appears to count from creation rather than start: a
test created nine days before launch and live for one day reports 10. It also disagrees with actual
runtime on tests that were paused and restarted. Compute days live from `startedAtTs` (on
`search_experiments`) instead.

**The primary metric is not always RPV.** It varies per test — as of the 2026-08-27 pull, three of
ten live tests were primaried on ATC while the rest used RPV. Read the `experienceKeyMetrics` entry
with `isPrimary: true` and map its `standardEventId` (`conversionRate`, `netRevenuePerVisitor`,
`netRevenuePerOrder`, `addToCartRate`, `profitPerVisitor`). If it genuinely can't be verified, set
`"assumed": true` on that test's `hero_metric` so the label renders with an asterisk — never pass a
guess off as verified.

**`get_variation_overview` does not expose `add_to_cart_rate`.** That's why `analyze_experience` is
the call to make. Don't split the pull across both tools.

**Responses can be enormous.** Variations carrying onsite edits return the injected HTML/CSS/JS
inline, and both tools can blow past the tool-output limit. When that happens the result is written
to a file and you extract what you need with `jq` — for `get_experience` that's just
`.experience.experienceKeyMetrics`.

**Parameter naming is inconsistent.** Most tools take the UUID as `id`; `analyze_experience` and
`generate_intelligems_graph` take it as `experienceId`. On a validation error, retry with the other.

**There is no checkout-completion metric.** The API returns the abandonment side,
`abandoned_checkout_rate` (abandoned checkouts ÷ checkouts started). Store that as pulled; the build
script renders the completion column as `1 − rate` and recomputes its lift from the two completion
values. Don't reuse the API's uplift on the abandonment metric for it — the lift of a complement is
not the negated lift of the source, and the two only coincide when the rate sits near 50%.

**A test with a 0%-allocated control returns `verdict: "error"`** — `"control variation is not
present in the series"` — and no uplift figures at all. Render the raw levels, mark readiness
`needs_time`, and say in the note that there are no comparisons.

**Group tags in test names can lie.** A test named `[ALL] …` may page-target a single URL. Check
`experiencePageTargeting` before trusting the bracket tag, and pass an explicit `"groups": [...]`
override when they disagree. Never substitute a *similarly*-named traffic group — a tag with no
match in the sheet counts as 0%, and that 0% is reported with a reason on the expanded row.

---

## Automating it

The better long-term shape is a scheduled GitHub Action that runs the pull and build in-repo, taking
the local session out of the loop entirely. That needs an Intelligems API credential as a repo
secret. Not built yet.

---

## Notes

- The repo is `firstday-alex/ab-test-dashboard` — spelled with the `h`. Earlier notes in `CLAUDE.md`
  and the skill's `SKILL.md` claimed `ab-test-dasboard`; that was wrong, verified against
  `git remote -v` on 2026-08-20.
- GitHub Pages is enabled, serving `main` at root.
- The Intelligems deep-link template (`IG_URL_TEMPLATE` in the build script) is **unverified**.
  `app.intelligems.io` is a single-page app returning HTTP 200 for every path, so fetching can't
  confirm the right shape. If a link 404s inside the app, copy one real URL out of the browser
  address bar and set the template — don't probe for it.
- API behaviors above were verified against the live account on 2026-08-19, re-confirmed 2026-08-20
  and 2026-08-27. Re-check `runtime_days` and the key-metric shape if Intelligems ships a new API
  version.
