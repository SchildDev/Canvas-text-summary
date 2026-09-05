#!/usr/bin/env python3
"""
Daily Canvas assignment notifier (ntfy.sh version).

Reads a Canvas ICS calendar feed (no API token needed), figures out what's
due, and sends up to 3 push notifications via ntfy.sh (free, no account
needed), grouped by urgency:
  ⏰ Overdue assignments  - one combined message, read-only
  (individual notifications) - one per item due today/tomorrow, each with
                                tap buttons: Complete, Snooze, Draft (tapping
                                the notification itself opens Canvas)
  🔭 Coming up            - one combined message, read-only, everything
                                due later (within FUTURE_DAYS)

Tapping Complete/Snooze/Draft fires an HTTPS request straight from your
phone to GitHub's API, which triggers the handle-assignment-action workflow
(see .github/workflows/handle-assignment-action.yml + handle_action.py) to
update state.json (and, for Draft, to generate a Claude-drafted starting
point and text it back to you). No server of your own required.

Required environment variables (set as GitHub Actions secrets):
  CANVAS_ICS_URL        - your Canvas calendar feed URL (https://..., not webcal://)
  NTFY_TOPIC            - the topic name you subscribed to in the ntfy app
  GITHUB_REPO           - "yourusername/canvas-daily-text"
  GITHUB_DISPATCH_TOKEN - a GitHub personal access token (classic, "repo" scope,
                           or fine-grained scoped to just this repo) used inside
                           the notification's tap-button requests. This token
                           travels through ntfy's relay and lives on your phone,
                           so scope it as narrowly as possible.

Optional:
  FUTURE_DAYS               - how far ahead the "Coming up" digest looks (default: 14)
  LOOKBACK_DAYS             - how far back overdue items are still tracked (default: 14)
  STATE_PATH                - path to the state file (default: state.json)
  SNOOZE_HOURS              - how many hours the Snooze button hides an item
                               for (default: 2). Since notifications only go
                               out on the daily schedule, this really means
                               "hidden until the next scheduled run that's at
                               least this many hours later," not an exact
                               countdown.
  ANTHROPIC_API_KEY         - if set, notifications get a one-line plain-English
                               summary of the assignment (via Claude) instead of
                               Canvas's raw instructor text. Summarized once per
                               assignment and cached in state.json.
  CANVAS_ANNOUNCEMENT_FEEDS - comma-separated list of per-course Canvas
                               announcement RSS/Atom feed URLs. Find each one
                               on a course's Announcements page (usually an
                               RSS icon/link). Only announcements published
                               within ANNOUNCEMENT_LOOKBACK_DAYS get sent, and
                               only once each (tracked in state.json). If
                               unset, announcement checking is skipped
                               entirely.
  ANNOUNCEMENT_LOOKBACK_DAYS - how recent an announcement must be to notify
                               about it (default: 3)
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, date, timezone
from urllib.parse import urlparse

import feedparser
import requests
from icalendar import Calendar

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
    return {"assignments": {}}


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
    done items keep showing up (bounded by lookback_days) until you tap
    Done for them.
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
    sentence for the notification body. Returns None on any failure so the
    caller can fall back to the raw stripped description instead of breaking
    the whole run over a summarization hiccup.
    """
    if not raw_description.strip():
        return None

    prompt = (
        "Summarize this Canvas assignment in one short, plain sentence "
        "(under 25 words) suitable for a push notification. Say what the "
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
    actually 'active' (not done, not currently snoozed) for today's
    notifications. Un-snoozes anything whose snooze period has passed.
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

        if anthropic_api_key and event["detail"] and not record.get("ai_summary"):
            ai_summary = summarize_with_claude(anthropic_api_key, event["summary"], event["detail"])
            if ai_summary:
                record["ai_summary"] = ai_summary

        if record["status"] == "snoozed":
            snooze_until = record.get("snooze_until")
            if snooze_until and snooze_until <= datetime.now(timezone.utc).isoformat():
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


def due_label(due, today):
    if due == today:
        return "Due today"
    if due == today + timedelta(days=1):
        return "Due tomorrow"
    if due < today:
        return f"Overdue since {due.strftime('%a %m/%d')}"
    return f"Due {due.strftime('%a %m/%d')}"


def escape_action_field(value):
    """ntfy's Actions header uses commas/semicolons as delimiters; escape them
    in any field we inject (labels, JSON bodies, etc.)."""
    return value.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;")


def build_actions(event, github_repo, github_token, snooze_hours):
    """
    Build the ntfy Actions header value: up to 3 tap buttons.
      1. Complete -> HTTP POST to GitHub's repository_dispatch API
      2. Snooze   -> same, different event_type
      3. Draft    -> same, triggers a Claude-drafted starting point

    "Open in Canvas" isn't one of these — it's set separately via the ntfy
    Click header (see send_ntfy_notification), which is the notification's
    default tap target and doesn't consume one of the 3 button slots.

    See: https://docs.ntfy.sh/publish/#action-buttons
    """
    dispatch_url = f"https://api.github.com/repos/{github_repo}/dispatches"
    actions = []

    for label, event_type, extra_payload in [
        ("Complete", "assignment_done", {}),
        (f"Snooze {snooze_hours}h", "assignment_snooze", {"snooze_hours": snooze_hours}),
        ("Draft", "assignment_draft", {}),
    ]:
        payload = {"event_type": event_type, "client_payload": {"id": event["id"], **extra_payload}}
        body = escape_action_field(json.dumps(payload))
        actions.append(
            f'http, "{label}", {dispatch_url}, method=POST, '
            f'headers.Authorization="Bearer {github_token}", '
            f'headers.Accept="application/vnd.github+json", '
            f'body=\'{body}\''
        )

    return "; ".join(actions)


def build_group_message(events, calendar_link):
    """Combine several events into one readable digest message, for the
    overdue and future buckets where individual tap-to-act notifications
    would be too noisy."""
    lines = []
    for event in events:
        line = f"• {event['summary']}"
        if event.get("ai_summary"):
            line += f"\n   {event['ai_summary']}"
        lines.append(line)

    message = "\n".join(lines)
    actions_header = f'view, "Open Canvas Calendar", {calendar_link}' if calendar_link else None
    return message, actions_header


def bucket_by_urgency(active, today):
    """Split into overdue / due-today-or-tomorrow / future, preserving the
    ascending due-date order already established by collect_events."""
    tomorrow = today + timedelta(days=1)
    overdue, soon, future = [], [], []

    for event in active:
        due = date.fromisoformat(event["due"])
        if due < today:
            overdue.append(event)
        elif due in (today, tomorrow):
            soon.append(event)
        else:
            future.append(event)

    return overdue, soon, future


def send_ntfy_notification(topic, title, message, actions_header=None, click_url=None, timeout=20):
    headers = {"Title": title.encode("utf-8")}
    if actions_header:
        headers["Actions"] = actions_header.encode("utf-8")
    if click_url:
        headers["Click"] = click_url.encode("utf-8")

    resp = requests.post(
        f"https://ntfy.sh/{topic}",
        data=message.encode("utf-8"),
        headers=headers,
        timeout=timeout,
    )
    resp.raise_for_status()


def fetch_new_announcements(feed_urls, state, lookback_days, timeout=20):
    """
    Check each course's announcement feed for anything new and recent.

    Uses two guards together: a recency window (so the very first run
    doesn't dump a whole semester's history at you) and a seen-ids set in
    state.json (so an announcement inside that window doesn't get repeated
    on every subsequent run just because it's still recent).
    """
    seen_ids = set(state.setdefault("seen_announcements", []))
    floor = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    new_items = []

    for feed_url in feed_urls:
        feed_url = feed_url.strip()
        if not feed_url:
            continue

        try:
            parsed = feedparser.parse(feed_url)
        except Exception as exc:  # noqa: BLE001 - one bad feed shouldn't kill the run
            print(f"WARNING: couldn't parse announcement feed {feed_url}: {exc}", file=sys.stderr)
            continue

        course_name = str(parsed.feed.get("title", "Canvas")).replace(" Announcements", "")

        for entry in parsed.entries:
            entry_id = entry.get("id") or entry.get("link")
            if not entry_id or entry_id in seen_ids:
                continue

            published_struct = entry.get("published_parsed")
            if published_struct:
                published_dt = datetime(*published_struct[:6], tzinfo=timezone.utc)
            else:
                published_dt = None

            # Always mark as seen once processed, whether or not it's recent
            # enough to notify about — this is what prevents old announcements
            # from ever surfacing, on this run or any future one.
            seen_ids.add(entry_id)

            if published_dt and published_dt < floor:
                continue

            content = entry.get("content", [{}])
            body_html = content[0].get("value") if content else entry.get("summary", "")
            body = strip_html(str(body_html), max_len=300)

            new_items.append({
                "course": course_name,
                "title": str(entry.get("title", "New announcement")),
                "body": body,
                "link": entry.get("link"),
            })

    state["seen_announcements"] = sorted(seen_ids)
    return new_items


def send_announcement_notifications(topic, announcements, timeout=20):
    for item in announcements:
        title = f"📢 {item['course']}: {item['title']}"
        message = item["body"] or "(no additional text)"
        actions_header = f'view, "Open in Canvas", {item["link"]}' if item.get("link") else None
        print(f"  -> announcement: {item['title']} ({item['course']})")
        send_ntfy_notification(topic, title, message, actions_header, timeout=timeout)


def main():
    ics_url = get_env("CANVAS_ICS_URL")
    ntfy_topic = get_env("NTFY_TOPIC")
    github_repo = get_env("GITHUB_REPO")
    github_dispatch_token = get_env("GITHUB_DISPATCH_TOKEN")
    future_days = int(get_env("FUTURE_DAYS", required=False, default="14"))
    lookback_days = int(get_env("LOOKBACK_DAYS", required=False, default="14"))
    state_path = get_env("STATE_PATH", required=False, default=STATE_PATH_DEFAULT)
    snooze_hours = int(get_env("SNOOZE_HOURS", required=False, default="2"))
    anthropic_api_key = get_env("ANTHROPIC_API_KEY", required=False, default=None)
    announcement_feeds_raw = get_env("CANVAS_ANNOUNCEMENT_FEEDS", required=False, default="")
    announcement_lookback_days = int(get_env("ANNOUNCEMENT_LOOKBACK_DAYS", required=False, default="3"))

    base_domain = urlparse(ics_url).netloc
    calendar_link = f"https://{base_domain}/calendar"

    state = load_state(state_path)

    # --- Announcements (independent of the assignment pipeline below) ---
    if announcement_feeds_raw.strip():
        feed_urls = announcement_feeds_raw.split(",")
        new_announcements = fetch_new_announcements(feed_urls, state, announcement_lookback_days)
        if new_announcements:
            print(f"--- Sending {len(new_announcements)} announcement notification(s) ---")
            send_announcement_notifications(ntfy_topic, new_announcements)
        else:
            print("No new announcements.")

    # --- Assignments ---
    cal = fetch_calendar(ics_url)
    # Fetch a wide window (up to future_days ahead) so the "future" bucket has
    # something to show; bucketing by urgency happens afterward.
    events, today = collect_events(cal, future_days, base_domain, lookback_days=lookback_days)
    active = apply_state(events, state, today, anthropic_api_key=anthropic_api_key)
    save_state(state_path, state)

    if not active:
        print("Nothing due — sending the all-clear notification.")
        send_ntfy_notification(
            ntfy_topic,
            "📚 Canvas check",
            f"Nothing due in the next {future_days} days. Enjoy the breather!",
        )
        return

    overdue, soon, future = bucket_by_urgency(active, today)

    if overdue:
        message, actions_header = build_group_message(overdue, calendar_link)
        print(f"--- Sending overdue digest ({len(overdue)} item(s)) ---")
        send_ntfy_notification(ntfy_topic, "⏰ Overdue assignments", message, actions_header)

    if soon:
        print(f"--- Sending {len(soon)} individual notification(s) for today/tomorrow ---")
        for event in soon:
            due = date.fromisoformat(event["due"])
            label = due_label(due, today)
            body_lines = [label]
            if event.get("ai_summary"):
                body_lines.append(event["ai_summary"])
            elif event.get("detail"):
                body_lines.append(event["detail"][:200])
            message = "\n".join(body_lines)

            actions_header = build_actions(event, github_repo, github_dispatch_token, snooze_hours)
            print(f"  -> {event['summary']} ({label})")
            send_ntfy_notification(ntfy_topic, event["summary"], message, actions_header, click_url=event.get("link"))

    if future:
        message, actions_header = build_group_message(future, calendar_link)
        print(f"--- Sending future digest ({len(future)} item(s)) ---")
        send_ntfy_notification(ntfy_topic, "🔭 Coming up", message, actions_header)

    print("------------------------")


if __name__ == "__main__":
    main()
