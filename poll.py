"""
Off-platform vendor status poller.

Runs on GitHub Actions cron (NOT on Railway — so a Railway outage cannot take
down the thing that detects Railway outages). Polls each vendor's Atlassian
Statuspage JSON, detects changes against committed state, and alerts Slack on
any change. Detection only — does not keep any service running. Collapses
time-to-knowing from ~70 min (stranded records) to ~5 min (poll interval).

COST DESIGN (deliberate):
  - Change-detection state lives in a committed `state.json` in this repo — FREE,
    and avoids waking any database every run.
  - Neon (FRIDAY visibility) is written ONLY when status changes — rare — so it
    does not keep FRIDAY's autoscaled DB awake 24/7.
  - Intended for a PUBLIC repo so GitHub Actions minutes are unlimited/free.

Env vars:
  SLACK_WEBHOOK_URL   required — incoming webhook for the ops channel
  NEON_DATABASE_URL   optional — if set, current state of changed vendors is
                      written to FRIDAY's infra_status table for "what's going
                      on?" queries. Omit to run Slack-only at zero DB cost.

Exit code is always 0 unless misconfigured — a vendor being down is not a
poller failure.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

STATE_FILE = Path(__file__).parent / "state.json"

# --- vendor list -------------------------------------------------------------
# All standard Atlassian Statuspage (/api/v2/status.json) — same response shape:
#   {"status": {"indicator": "none|minor|major|critical", "description": "..."}}
# CONFIRMED = verified returning valid JSON. VERIFY = plausible but unconfirmed;
# the poller reports 'unknown' (not a crash) if the endpoint misbehaves.
VENDORS = [
    # Railway's custom domain (status.railway.com) serves HTML, not the API.
    # The real Atlassian Statuspage API is on the statuspage.io subdomain.
    {"name": "Railway",    "api": "https://railway.statuspage.io/api/v2/status.json",    "page": "https://status.railway.com",       "verified": True},
    {"name": "OpenAI",     "api": "https://status.openai.com/api/v2/status.json",        "page": "https://status.openai.com",        "verified": True},
    {"name": "Anthropic",  "api": "https://status.anthropic.com/api/v2/status.json",     "page": "https://status.anthropic.com",     "verified": True},
    {"name": "Cloudflare", "api": "https://www.cloudflarestatus.com/api/v2/status.json", "page": "https://www.cloudflarestatus.com", "verified": True},
    {"name": "GitHub",     "api": "https://www.githubstatus.com/api/v2/status.json",     "page": "https://www.githubstatus.com",     "verified": True},
    # --- NOT YET SUPPORTED (need custom adapters, not Atlassian Statuspage) ---
    # Neon: neonstatus.com is a custom page (not Atlassian/Instatus). No standard
    #   /api/v2/status.json. Matters to us (FRIDAY + all app DBs) — TODO custom adapter.
    # NetSuite/Oracle: uses Oracle Trust portal, not Atlassian. TODO custom adapter.
]

SEVERITY = {"none": "🟢", "minor": "🟡", "major": "🟠", "critical": "🔴", "unknown": "⚪"}
RANK = {"none": 0, "unknown": 1, "minor": 2, "major": 3, "critical": 4}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_status(vendor: dict) -> tuple[str, str]:
    """Return (indicator, description). Never raises — failure => ('unknown', reason)."""
    try:
        r = requests.get(vendor["api"], timeout=15, headers={"User-Agent": "aiterated-status-poller/1"})
        if r.status_code != 200:
            return "unknown", f"HTTP {r.status_code} from status endpoint"
        status = r.json().get("status", {})
        indicator = status.get("indicator", "unknown")
        if indicator not in SEVERITY:
            indicator = "unknown"
        return indicator, status.get("description", "")
    except requests.exceptions.Timeout:
        return "unknown", "status endpoint timed out (15s)"
    except requests.exceptions.RequestException as e:
        return "unknown", f"request failed: {type(e).__name__}"
    except (ValueError, KeyError) as e:
        return "unknown", f"unparseable status response: {type(e).__name__}"


def load_state() -> dict[str, dict]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def save_state(state: dict[str, dict]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def post_slack(webhook: str, text: str) -> None:
    try:
        r = requests.post(webhook, json={"text": text}, timeout=15)
        if r.status_code >= 300:
            print(f"  WARN: Slack returned HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
    except requests.exceptions.RequestException as e:
        print(f"  WARN: Slack post failed: {type(e).__name__}", file=sys.stderr)


def write_neon(changes: list[tuple], neon_url: str) -> None:
    """Write changed vendors' current state to FRIDAY's infra_status. Only called
    on change, so it does not keep the DB awake. Best-effort."""
    try:
        import psycopg2
    except ImportError:
        print("  WARN: psycopg2 not installed; skipping Neon write", file=sys.stderr)
        return
    try:
        conn = psycopg2.connect(neon_url)
        conn.autocommit = True
        with conn.cursor() as cur:
            for name, _old, new, desc, page in changes:
                cur.execute(
                    """
                    INSERT INTO infra_status (vendor, indicator, description, status_url, last_check, last_change)
                    VALUES (%s, %s, %s, %s, now(), now())
                    ON CONFLICT (vendor) DO UPDATE SET
                        indicator = EXCLUDED.indicator,
                        description = EXCLUDED.description,
                        status_url = EXCLUDED.status_url,
                        last_check = now(),
                        last_change = now()
                    """,
                    (name, new, desc, page),
                )
        conn.close()
    except Exception as e:  # noqa: BLE001 — never let a DB issue crash the poller
        print(f"  WARN: Neon write failed: {type(e).__name__}: {e}", file=sys.stderr)


def main() -> int:
    slack_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not slack_url:
        print("ERROR: SLACK_WEBHOOK_URL env var required", file=sys.stderr)
        return 2
    neon_url = os.environ.get("NEON_DATABASE_URL")  # optional

    state = load_state()
    changes = []
    snapshot = []

    for v in VENDORS:
        name = v["name"]
        indicator, description = fetch_status(v)
        snapshot.append((name, indicator, description))

        prev = state.get(name)
        prev_indicator = prev["indicator"] if prev else None

        changed = prev_indicator is not None and prev_indicator != indicator
        first_seen_bad = prev_indicator is None and indicator != "none"

        if changed or first_seen_bad:
            old_label = prev_indicator if prev_indicator is not None else "(first check)"
            changes.append((name, old_label, indicator, description, v["page"]))
            state[name] = {"indicator": indicator, "description": description, "last_change": now_iso()}
        elif prev is None:
            # first check, currently healthy — record baseline without alerting
            state[name] = {"indicator": indicator, "description": description, "last_change": now_iso()}
        else:
            state[name]["description"] = description  # keep desc fresh, no alert

    # Console summary (always — shows in Actions log)
    print(f"=== status poll {now_iso()} ===")
    for name, indicator, desc in snapshot:
        print(f"  {SEVERITY.get(indicator, '⚪')} {name:<12} {indicator:<9} {desc}")

    if changes:
        lines = ["*⚠️ Infra status change detected*", ""]
        for name, old, new, desc, page in changes:
            worsened = RANK.get(new, 1) > RANK.get(old if old in RANK else "none", 0)
            arrow = "↗️" if worsened else "↘️"
            lines.append(f"{SEVERITY.get(new, '⚪')} *{name}*: {old} {arrow} {new}")
            if desc:
                lines.append(f"    {desc}")
            lines.append(f"    {page}\n")
        post_slack(slack_url, "\n".join(lines).strip())
        if neon_url:
            write_neon(changes, neon_url)
        print(f"\n  -> {len(changes)} change(s): Slack alerted" + (", Neon updated" if neon_url else ""))
    else:
        print("\n  -> no changes; Slack quiet, no DB write")

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
