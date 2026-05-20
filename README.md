# status-poller

Off-platform vendor status poller for the Aiterated / HCL fleet. Detects vendor
outages (Railway, OpenAI, Anthropic, Cloudflare, Neon, GitHub) within ~5 minutes
and alerts Slack — so we learn about an outage from the system, not from stranded
records 70 minutes later.

**Detection only.** It does not keep any service running. Its value is collapsing
time-to-knowing, which buys: faster customer comms, no time wasted investigating a
code path when the cause is upstream, and an outage log in FRIDAY for the post-mortem.

Built 2026-05-20 after the 2026-05-19 Railway outage stranded 9 HCL SOs and took
30 minutes to (mis)diagnose. See FRIDAY `claude-code/reference/check-status-page-before-code-path`.

## Why it runs on GitHub Actions, not Railway

If the poller ran on Railway, a Railway outage would take down the thing meant to
detect Railway outages. GitHub Actions runs on GitHub's infrastructure — an
independent failure domain — and is free for public repos.

## Cost: ~$0

| Resource | Cost | Why |
|---|---|---|
| GitHub Actions minutes | **$0** | Public repos get unlimited free Actions minutes. (A private repo at 5-min cadence would bill ~$53/mo over the 2,000-min free tier — so keep this repo PUBLIC.) |
| Neon (FRIDAY visibility) | **~$0** | State lives in committed `state.json`, not the DB. Neon is written ONLY when a status changes (rare), so it never keeps FRIDAY's autoscaled DB awake. Omit `NEON_DATABASE_URL` entirely to run Slack-only at exactly $0. |
| Slack | $0 | Incoming webhook. |

No secrets live in the code (they're in Actions Secrets), so a public repo is safe.

## Setup

1. **Create a PUBLIC GitHub repo** and push this directory:
   ```
   cd C:\Users\micha\repo\status-poller
   git init
   git add .
   git commit -m "feat: off-platform vendor status poller"
   gh repo create Metrix-Digital/status-poller --public --source=. --push
   ```
   (or create the repo in the GitHub UI and `git remote add origin ... && git push -u origin main`)

2. **Add the Slack webhook secret** (required):
   ```
   gh secret set SLACK_WEBHOOK_URL --body "https://hooks.slack.com/services/XXX/YYY/ZZZ"
   ```
   Create the webhook at https://api.slack.com/apps → your app → Incoming Webhooks → point it at the ops channel.

3. **Optionally add the Neon secret** (for FRIDAY "what's going on?" visibility):
   ```
   gh secret set NEON_DATABASE_URL --body "postgres://USER:PASS@HOST/neondb?sslmode=require"
   ```
   This is the FRIDAY project (`wispy-thunder-27280753`). The `infra_status` table already exists. Skip this secret to run Slack-only.

4. **Trigger a first run** to seed the baseline:
   ```
   gh workflow run status-poll
   ```
   First run records current state for every vendor without alerting (unless something is already red). Subsequent runs alert only on change.

## Local test

```
$env:SLACK_WEBHOOK_URL = "https://hooks.slack.com/services/..."
# NEON_DATABASE_URL optional
python poll.py
```

Prints a live snapshot table and posts to Slack only if status changed vs `state.json`.

## Adding / verifying vendors

Edit the `VENDORS` list in `poll.py`. All standard Atlassian Statuspage endpoints
share the `/api/v2/status.json` shape. `verified: True` means the endpoint was
confirmed returning valid JSON; `False` means plausible-but-unconfirmed (the poller
reports `unknown` rather than crashing if such an endpoint misbehaves — watch for a
vendor stuck at `unknown`, which means its URL needs fixing, not that it's down).

**Confirmed working** (verified 2026-05-20): Railway, OpenAI, Anthropic, Cloudflare, GitHub.
  - Note: Railway's API is at `railway.statuspage.io`, NOT `status.railway.com` (the custom
    domain serves HTML, not the JSON API).
**Not yet supported** (need custom adapters — not Atlassian Statuspage):
  - **Neon** — `neonstatus.com` is a custom status page with no standard `/api/v2/status.json`.
    Matters to us (FRIDAY + all app DBs run on Neon), so this is a real follow-up, not a skip.
  - **NetSuite/Oracle** — uses the Oracle Trust portal, not Atlassian.

## Indicator levels

`none` 🟢 · `minor` 🟡 · `major` 🟠 · `critical` 🔴 · `unknown` ⚪ (endpoint unreachable/unparseable)

## Limitations

- GitHub scheduled workflows can be delayed under load (best-effort, usually 5-15 min).
  For sub-minute detection you'd need a dedicated always-on poller (off-Railway) — out
  of scope for v1.
- `unknown` for a vendor can mean either the vendor's status page is down OR the URL is
  wrong. During a real outage, status pages sometimes go down too, so `unknown` is itself
  a weak signal worth glancing at.
