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
# kind "atlassian" (the default): Atlassian Statuspage /api/v2/status.json —
#   {"status": {"indicator": "none|minor|major|critical", "description": "..."}}
# kind "railway": Railway's own status site (/api/status), see parse_railway().
#   Railway left Atlassian; railway.statuspage.io has been frozen at "All
#   Systems Operational" since 2026-10-01 and missed the 2026-10-20 builds outage.
# CONFIRMED = verified returning valid JSON. VERIFY = plausible but unconfirmed;
# the poller reports 'unknown' (not a crash) if the endpoint misbehaves.
VENDORS = [
    {"name": "Railway",    "api": "https://status.railway.com/api/status",               "page": "https://status.railway.com",       "verified": True, "kind": "railway"},
    {"name": "OpenAI",     "api": "https://status.openai.com/api/v2/status.json",        "page": "https://status.openai.com",        "verified": True},
    # status.anthropic.com 301-redirects to status.claude.com; poll the target directly.
    {"name": "Anthropic",  "api": "https://status.claude.com/api/v2/status.json",        "page": "https://status.claude.com",        "verified": True},
    {"name": "Cloudflare", "api": "https://www.cloudflarestatus.com/api/v2/status.json", "page": "https://www.cloudflarestatus.com", "verified": True},
    {"name": "GitHub",     "api": "https://www.githubstatus.com/api/v2/status.json",     "page": "https://www.githubstatus.com",     "verified": True},
    # --- NOT YET SUPPORTED (need custom adapters, not Atlassian Statuspage) ---
    # Neon: neonstatus.com is a custom page (not Atlassian/Instatus). No standard
    #   /api/v2/status.json. Matters to us (FRIDAY + all app DBs) — TODO custom adapter.
    # NetSuite/Oracle: uses Oracle Trust portal, not Atlassian. TODO custom adapter.
]

SEVERITY = {"none": "🟢", "minor": "🟡", "major": "🟠", "critical": "🔴", "unknown": "⚪"}
RANK = {"none": 0, "unknown": 1, "minor": 2, "major": 3, "critical": 4}


USER_AGENT = "aiterated-status-poller/1"

# Railway component impact -> indicator. Unknown strings fall back to "minor".
RAILWAY_IMPACT = {
    "MAJOR_OUTAGE": "major",
    "FULL_OUTAGE": "critical",
    "OUTAGE": "critical",
    "CRITICAL": "critical",
    "PARTIAL_OUTAGE": "minor",
    "DEGRADED": "minor",
    "DEGRADED_PERFORMANCE": "minor",
}
DESCRIPTION_MAX = 300

# Migration guard: each Atlassian vendor's page is re-checked for the
# "statuspage" marker at most once per interval; bookkeeping lives in state.json.
PAGE_GUARD_KEY = "_page_guard"
PAGE_GUARD_INTERVAL_S = 24 * 3600


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_railway(data: dict) -> tuple[str, str]:
    """Map Railway's /api/status JSON to (indicator, description).

    The worst component impact across all active incidents wins. A malformed
    body raises KeyError/TypeError/AttributeError, which fetch_status catches.
    """
    incidents = data["activeIncidents"]
    if not isinstance(incidents, list):
        # a null/changed shape must read as unknown, never as healthy
        raise TypeError(f"activeIncidents is {type(incidents).__name__}, expected list")
    if not incidents:
        return "none", "All Systems Operational"
    worst = "minor"  # an active incident with no mapped component is still an incident
    parts = []
    for inc in incidents:
        parts.append(f"{inc.get('title', 'Untitled incident')} ({inc.get('status', 'UNKNOWN')})")
        for comp in inc.get("components") or []:
            impact = comp.get("impact")
            indicator = RAILWAY_IMPACT.get(impact)
            if indicator is None:
                print(f"  WARN: Railway impact {impact!r} unmapped; treating as minor", file=sys.stderr)
                indicator = "minor"
            if RANK[indicator] > RANK[worst]:
                worst = indicator
    description = "; ".join(parts)
    if len(description) > DESCRIPTION_MAX:
        description = description[: DESCRIPTION_MAX - 3] + "..."
    return worst, description


def page_looks_atlassian(page: str) -> bool | None:
    """Whether the page HTML mentions "statuspage"; None when the fetch fails."""
    try:
        r = requests.get(page, timeout=15, headers={"User-Agent": USER_AGENT})
        if r.status_code != 200:
            print(f"  WARN: page guard: HTTP {r.status_code} from {page}", file=sys.stderr)
            return None
        return "statuspage" in r.text.lower()
    except requests.exceptions.RequestException as e:
        print(f"  WARN: page guard: fetch of {page} failed: {type(e).__name__}", file=sys.stderr)
        return None


def check_page_migrations(state: dict, slack_url: str, now: datetime) -> None:
    """At most once a day, confirm each Atlassian vendor's page is still a
    Statuspage. Warn Slack once per vendor when it is not (the poller's source
    may be frozen, as Railway's was); clear the flag when the page changes back."""
    guard = state.setdefault(PAGE_GUARD_KEY, {"last_check": None, "warned": []})
    try:
        elapsed = (now - datetime.fromisoformat(guard["last_check"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        elapsed = None  # never checked, or unreadable timestamp: check now
    if elapsed is not None and elapsed < PAGE_GUARD_INTERVAL_S:
        return
    warned = set(guard.get("warned") or [])
    for v in VENDORS:
        if v.get("kind", "atlassian") != "atlassian":
            continue
        looks = page_looks_atlassian(v["page"])
        if looks is None:
            continue
        if looks:
            warned.discard(v["name"])
        elif v["name"] not in warned and post_slack(
            slack_url,
            f"{v['name']} status page no longer looks like Atlassian Statuspage: "
            f"the poller's source may be stale, verify {v['page']}",
        ):
            warned.add(v["name"])  # only once delivered; a failed post retries at the next daily check
    guard["last_check"] = now.isoformat()
    guard["warned"] = sorted(warned)


def fetch_status(vendor: dict) -> tuple[str, str]:
    """Return (indicator, description). Never raises — failure => ('unknown', reason)."""
    try:
        r = requests.get(vendor["api"], timeout=15, headers={"User-Agent": USER_AGENT})
        if r.status_code != 200:
            return "unknown", f"HTTP {r.status_code} from status endpoint"
        if vendor.get("kind", "atlassian") == "railway":
            return parse_railway(r.json())
        status = r.json().get("status", {})
        indicator = status.get("indicator", "unknown")
        if indicator not in SEVERITY:
            indicator = "unknown"
        return indicator, status.get("description", "")
    except requests.exceptions.Timeout:
        return "unknown", "status endpoint timed out (15s)"
    except requests.exceptions.RequestException as e:
        return "unknown", f"request failed: {type(e).__name__}"
    except (ValueError, KeyError, TypeError, AttributeError) as e:
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


def post_slack(webhook: str, text: str) -> bool:
    """Post to Slack. Returns True only when Slack accepted the message."""
    try:
        r = requests.post(webhook, json={"text": text}, timeout=15)
        if r.status_code >= 300:
            print(f"  WARN: Slack returned HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
            return False
        return True
    except requests.exceptions.RequestException as e:
        print(f"  WARN: Slack post failed: {type(e).__name__}", file=sys.stderr)
        return False


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

    check_page_migrations(state, slack_url, datetime.now(timezone.utc))
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
