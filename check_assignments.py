#!/usr/bin/env python3
"""
Daily Canvas assignment texter.

Reads a Canvas ICS calendar feed (no API key needed), figures out what's
due soon, and sends a text message via Twilio summarizing it. Each item is
numbered so you can text back:
  DONE 2    - mark item 2 as done, stop mentioning it
  SNOOZE 2  - hide item 2 for a few days, then bring it back
  INFO 2    - get more detail (assignment description + link) on item 2

Replies are handled separately by a Twilio Function (see
twilio-function/inbound-sms.js) which reads/writes state.json in this repo.
This script only needs to read state.json to know what to skip, and update
it with fresh data + today's numbering.

Required environment variables (set as GitHub Actions secrets):
  CANVAS_ICS_URL   - your Canvas calendar feed URL (https://..., not webcal://)
  TWILIO_SID       - Twilio Account SID
  TWILIO_AUTH      - Twilio Auth Token
  TWILIO_FROM      - Twilio phone number, e.g. +15551234567
  TWILIO_TO        - your phone number, e.g. +18505551234

Optional:
  LOOKAHEAD_DAYS      - how many days ahead to include (default: 2, i.e. today + tomorrow)
  STATE_PATH          - path to the state file (default: state.json)
  ANTHROPIC_API_KEY   - if set, INFO replies get a one-line plain-English
                         summary of the assignment (via Claude) instead of
                         Canvas's raw instructor text. Summarized once per
                         assignment and cached in state.json.
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, date
from urllib.parse import urlparse

import requests
from icalendar import Calendar
from twilio.rest import Client

STATE_PATH_DEFAULT = "state.json"

# Canvas calendar feeds mix real graded work in with lectures, office hours,
# and "no class" notices. These substrings (case-insensitive) mark events
# that aren't something to "work on" and should be filtered out.
NOISE_KEYWORDS = [
    "office hours",
    "lecture",
    "no class in lieu",
    "seating window opens",
]

COURSE_TAG_RE = re.compile(r"\s*\[([A-Z]{2,4}\d{3,4})[^\]]*\]\s*$")
COURSE_ID_RE = re.compile(r"course_(\d+)")
ASSIGNMENT_UID_RE = re.compile(r"event-assignment-(\d+)")
HTML_TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")


def get_env(name, required=True, default=None):
    val = os.environ.get(name, default)
    if required and not val:
        print(f"ERROR: missing required environment variable {name}", file=sys.stderr)
        sys.exit(1)
    return val


def fetch_calendar(ics_url):
    resp = requests.get(ics_url, timeout=30)
    resp.raise_for_status()
    return Calendar.from_ical(resp.content)


def to_date(dt_or_date):
    """Normalize icalendar's dt (datetime or date) down to a date."""
    if isinstance(dt_or_date, datetime):
        return dt_or_date.date()
    return dt_or_date


def clean_summary(summary):
    """Shorten '...[GEB1030-0023.fa26]' to '... (GEB1030)'."""
    match = COURSE_TAG_RE.search(summary)
    if not match:
        return summary
    course = match.group(1)
    base = COURSE_TAG_RE.sub("", summary).strip()
    return f"{base} ({course})"


def is_noise(summary):
    lower = summary.lower()
    return any(keyword in lower for keyword in NOISE_KEYWORDS)


def strip_html(text, max_len=300):
    text = HTML_TAG_RE.sub(" ", text)
    text = WHITESPACE_RE.sub(" ", text).strip()
    if len(text) > max_len:
        text = text[:max_len - 1].rstrip() + "…"
    return text


def get_assignment_id(component):
    """Stable ID used as the state.json key. Falls back to the raw UID for
    events that don't match the usual 'event-assignment-<id>' pattern."""
    uid = str(component.get("uid", ""))
    match = ASSIGNMENT_UID_RE.search(uid)
    return match.group(1) if match else uid


def build_direct_link(component, base_domain):
    """
    Canvas's ICS feed only gives a link to the calendar day, not the
    assignment page itself. But the UID (event-assignment-<id>) and the
    calendar URL's course_<id> together let us construct the real
    assignment page link, which is what you actually want to tap.
    Falls back to the calendar-day link if the pattern doesn't match.
    """
    calendar_url = str(component.get("url", ""))
    assignment_id = get_assignment_id(component)
    course_match = COURSE_ID_RE.search(calendar_url)

    if assignment_id.isdigit() and course_match:
        course_id = course_match.group(1)
        return f"https://{base_domain}/courses/{course_id}/assignments/{assignment_id}"

    return calendar_url or None


def load_state(state_path):
    if os.path.exists(state_path):
        with open(state_path, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                print(f"WARNING: {state_path} was invalid JSON, starting fresh", file=sys.stderr)
    return {"assignments": {}, "last_sent": {}}


def save_state(state_path, state):
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def collect_events(cal, lookahead_days, base_domain, lookback_days=14):
    """
    Parse the ICS feed into a flat list of candidate events (not yet
    filtered by done/snoozed status — that happens against state.json).

    Scans from `lookback_days` in the past through the lookahead window,
    not just today forward. This matters because we can't tell from the
    ICS feed whether something overdue was actually submitted — without
    this, a snoozed item whose due date passes while snoozed would vanish
    from tracking entirely instead of coming back. Overdue-and-not-marked-
    done items keep showing up (bounded by lookback_days) until you text
    DONE for them.
    """
    today = date.today()
    cutoff = today + timedelta(days=lookahead_days - 1)
    floor = today - timedelta(days=lookback_days)

    events = []
    for component in cal.walk("VEVENT"):
        dtstart = component.get("dtstart")
        if dtstart is None:
            continue
        due = to_date(dtstart.dt)
        if not (floor <= due <= cutoff):
            continue

        raw_summary = str(component.get("summary", "Untitled assignment"))
        if is_noise(raw_summary):
            continue

        events.append({
            "id": get_assignment_id(component),
            "summary": clean_summary(raw_summary),
            "link": build_direct_link(component, base_domain),
            "detail": strip_html(str(component.get("description", ""))),
            "due": due.isoformat(),
        })

    events.sort(key=lambda e: e["due"])
    return events, today


def summarize_with_claude(api_key, title, raw_description, timeout=20):
    """
    Turn Canvas's often-verbose instructor text into one short, plain-English
    sentence for a text message. Returns None on any failure so the caller
    can fall back to the raw stripped description instead of breaking the
    whole run over a summarization hiccup.
    """
    if not raw_description.strip():
        return None

    prompt = (
        "Summarize this Canvas assignment in one short, plain sentence "
        "(under 25 words) suitable for a text message. Say what the "
        "student actually has to do. No preamble, just the sentence.\n\n"
        f"Assignment title: {title}\n"
        f"Instructions: {raw_description[:2000]}"
    )

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        text_blocks = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
        summary = " ".join(text_blocks).strip()
        return summary or None
    except Exception as exc:  # noqa: BLE001 - never let a summarization hiccup kill the run
        print(f"WARNING: Claude summarization failed: {exc}", file=sys.stderr)
        return None


def apply_state(events, state, today, anthropic_api_key=None):
    """
    Merge fresh feed data into state.json, then decide which events are
    actually 'active' (not done, not currently snoozed) for today's text.
    Un-snoozes anything whose snooze period has passed.

    If an Anthropic API key is provided, generates a one-line plain-English
    summary for each assignment's description the first time it's seen, and
    caches it in state so we don't re-summarize (and re-pay for) it daily.
    """
    assignments = state.setdefault("assignments", {})
    active = []

    for event in events:
        record = assignments.get(event["id"], {})
        record.update({
            "summary": event["summary"],
            "link": event["link"],
            "detail": event["detail"],
            "due": event["due"],
        })
        record.setdefault("status", "pending")

        # Only summarize once per assignment, and only if we haven't already
        # (covers both first-time-seen and previously-failed summarization).
        if anthropic_api_key and event["detail"] and not record.get("ai_summary"):
            ai_summary = summarize_with_claude(anthropic_api_key, event["summary"], event["detail"])
            if ai_summary:
                record["ai_summary"] = ai_summary

        if record["status"] == "snoozed":
            snooze_until = record.get("snooze_until")
            if snooze_until and snooze_until <= today.isoformat():
                record["status"] = "pending"
                record.pop("snooze_until", None)

        assignments[event["id"]] = record

        if record["status"] == "pending":
            active.append({
                "id": event["id"],
                "summary": record["summary"],
                "link": record["link"],
                "detail": record["detail"],
                "ai_summary": record.get("ai_summary"),
                "due": record["due"],
            })

    return active


def build_message(active, today):
    if not active:
        return (
            "📚 Canvas check: nothing due today or tomorrow. Enjoy the breather!",
            {},
        )

    lines = ["📚 Canvas — what's due:"]
    current_day = None
    numbered = {}

    for i, event in enumerate(active, start=1):
        due = date.fromisoformat(event["due"])
        if due != current_day:
            current_day = due
            if due == today:
                label = "Today"
            elif due == today + timedelta(days=1):
                label = "Tomorrow"
            elif due < today:
                label = f"Overdue ({due.strftime('%a %m/%d')})"
            else:
                label = due.strftime("%a %m/%d")
            lines.append(f"\n{label}:")
        lines.append(f"{i}. {event['summary']}")
        if event.get("ai_summary"):
            lines.append(f"   {event['ai_summary']}")
        numbered[str(i)] = event["id"]

    lines.append("\nReply DONE #, SNOOZE #, or INFO # (e.g. DONE 2)")
    msg = "\n".join(lines)

    # SMS segments are cheap but not free/unlimited-length; keep it sane.
    if len(msg) > 1500:
        msg = msg[:1490] + "\n…(truncated)"
    return msg, numbered


def send_sms(body, sid, auth, from_number, to_number):
    client = Client(sid, auth)
    message = client.messages.create(body=body, from_=from_number, to=to_number)
    print(f"Sent message SID: {message.sid}")


def main():
    ics_url = get_env("CANVAS_ICS_URL")
    twilio_sid = get_env("TWILIO_SID")
    twilio_auth = get_env("TWILIO_AUTH")
    twilio_from = get_env("TWILIO_FROM")
    twilio_to = get_env("TWILIO_TO")
    lookahead_days = int(get_env("LOOKAHEAD_DAYS", required=False, default="2"))
    state_path = get_env("STATE_PATH", required=False, default=STATE_PATH_DEFAULT)
    anthropic_api_key = get_env("ANTHROPIC_API_KEY", required=False, default=None)

    base_domain = urlparse(ics_url).netloc

    cal = fetch_calendar(ics_url)
    events, today = collect_events(cal, lookahead_days, base_domain)

    state = load_state(state_path)
    active = apply_state(events, state, today, anthropic_api_key=anthropic_api_key)
    message, numbered = build_message(active, today)
    state["last_sent"] = numbered
    save_state(state_path, state)

    print("--- Message preview ---")
    print(message)
    print("------------------------")

    send_sms(message, twilio_sid, twilio_auth, twilio_from, twilio_to)


if __name__ == "__main__":
    main()
