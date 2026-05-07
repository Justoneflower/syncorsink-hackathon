#!/usr/bin/env python3
"""
trace.py — End-to-end audit trail for a single UBID.

Usage:
    ./scripts/trace.py KA-UBID-2025-0089123
    ./scripts/trace.py KA-UBID-2025-0089123 --since 5m
    ./scripts/trace.py KA-UBID-2025-0089123 --conflicts-only

This is the killer feature for evaluating SyncOrSink. One question — "what
happened to this business?" — gets one answer with the complete causal
chain: ingestion → routing lookup → translation → write → audit, plus
any conflicts and how they were resolved.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

DEFAULT_URL = "http://localhost:8000"

# ANSI color codes (gracefully degrade if not on a TTY)
if sys.stdout.isatty():
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    CYAN = "\033[36m"
    MAGENTA = "\033[35m"
    BLUE = "\033[34m"
    RESET = "\033[0m"
else:
    BOLD = DIM = GREEN = YELLOW = RED = CYAN = MAGENTA = BLUE = RESET = ""

ACTION_COLOR = {
    "ingest":         CYAN,
    "lookup":         BLUE,
    "translate":      MAGENTA,
    "write":          GREEN,
    "conflict":       YELLOW,
    "policy":         YELLOW,
    "review_queued":  YELLOW,
    "manual_resolve": GREEN,
    "propagate":      DIM,
    "retry":          YELLOW,
    "dlq":            RED,
}


def parse_since(s):
    if s is None:
        return None
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    n, unit = int(s[:-1]), s[-1]
    return timedelta(seconds=n * units[unit])


def fetch(ubid, base_url):
    url = f"{base_url}/trace/{ubid}"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return json.loads(r.read().decode())
    except urllib.error.URLError as e:
        sys.stderr.write(f"{RED}Could not reach middleware at {base_url}: {e}{RESET}\n")
        sys.stderr.write(f"  Is `docker-compose up` running?\n")
        sys.exit(1)


def fmt_ts(iso):
    """HH:MM:SS.ms — readable time component."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
    except Exception:
        return iso


def main():
    ap = argparse.ArgumentParser(description="Pretty-print the audit trail for a UBID.")
    ap.add_argument("ubid", help="UBID to trace, e.g. KA-UBID-2025-0089123")
    ap.add_argument("--url", default=DEFAULT_URL, help=f"middleware base URL (default: {DEFAULT_URL})")
    ap.add_argument("--since", help="only events from the last N (e.g. 5m, 1h, 30s)")
    ap.add_argument("--conflicts-only", action="store_true", help="only show conflict / policy events")
    ap.add_argument("--json", action="store_true", help="raw JSON output (machine-readable)")
    args = ap.parse_args()

    entries = fetch(args.ubid, args.url)

    if args.since:
        cutoff = datetime.now(timezone.utc) - parse_since(args.since)
        entries = [
            e for e in entries
            if datetime.fromisoformat(e["occurred_at"].replace("Z", "+00:00")) >= cutoff
        ]

    if args.conflicts_only:
        entries = [e for e in entries if e["action"] in ("conflict", "policy", "review_queued", "manual_resolve")]

    if args.json:
        print(json.dumps(entries, indent=2))
        return

    # Pretty header
    print()
    print(f"{BOLD}AUDIT TRAIL{RESET}  ubid={CYAN}{args.ubid}{RESET}  ({len(entries)} entries)")
    print("─" * 88)

    if not entries:
        print(f"  {DIM}(no events yet){RESET}")
        return

    # Group by event_id so causally-related lines group together
    by_event = {}
    order = []
    for e in entries:
        eid = e["event_id"]
        if eid not in by_event:
            by_event[eid] = []
            order.append(eid)
        by_event[eid].append(e)

    for eid in order:
        group = by_event[eid]
        first = group[0]
        head_color = CYAN if first["source"] == "sws" else MAGENTA
        print(f"\n  {DIM}event {eid[:12]}…  source={head_color}{first['source']}{RESET}")
        for e in group:
            ts = fmt_ts(e["occurred_at"])
            action = e["action"]
            color = ACTION_COLOR.get(action, "")
            target = e.get("target") or "-"
            status = e["status"] or ""
            status_color = GREEN if status == "ok" else (YELLOW if status == "skipped" else RED)
            line = f"    {DIM}{ts}{RESET}  {color}{action:<14}{RESET}  → {target:<11}  {status_color}{status}{RESET}"

            # Inline payload preview for the most informative actions
            if action in ("conflict", "policy", "review_queued") and e.get("payload"):
                p = e["payload"]
                if action == "conflict":
                    line += f"  {DIM}field={p.get('field')} vs {p.get('competing_source')} ({p.get('policy')}){RESET}"
                elif action == "policy":
                    line += f"  {DIM}field={p.get('field')} → {p.get('outcome')}{RESET}"
                elif action == "review_queued":
                    line += f"  {DIM}field={p.get('field')}{RESET}"
            elif action == "lookup" and e.get("payload", {}).get("depts"):
                line += f"  {DIM}depts={e['payload']['depts']}{RESET}"

            if e.get("error"):
                line += f"\n      {RED}error: {e['error']}{RESET}"
            print(line)

    print()


if __name__ == "__main__":
    main()
