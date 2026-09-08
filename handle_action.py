#!/usr/bin/env python3
"""
Handles a single Complete/Snooze/Draft/bulk-done action, triggered by
GitHub's repository_dispatch API (which the tap buttons in your ntfy
notifications call directly).

Reads the event type + assignment id(s) passed in by the workflow (as
environment variables — see .github/workflows/handle-assignment-action.yml)
and updates state.json accordingly. The workflow commits the change back
to the repo after this script runs.

Required environment variables:
  COMMAND         - "assignment_done", "assignment_snooze", "assignment_draft",
                     or "assignments_bulk_done"
  ASSIGNMENT_ID   - the assignment's id, for assignment_done/snooze/draft
  ASSIGNMENT_IDS  - comma-separated ids, for assignments_bulk_done

Optional:
  SNOOZE_HOURS       - how many hours to snooze for (default: 2). Since
                       notifications only go out on the daily schedule, this
                       really means "hidden until the next scheduled run
                       that's at least this many hours later."
  STATE_PATH         - path to the state file (default: state.json)
  ANTHROPIC_API_KEY  - required for "assignment_draft"
  GMAIL_ADDRESS      - required for "assignment_draft" — both the sender
                       and the recipient (the draft gets emailed to you at
                       this same address)
  GMAIL_APP_PASSWORD - required for "assignment_draft" — a Google App
                       Password (not your real password), generated at
                       myaccount.google.com/apppasswords, needs 2-Step
                       Verification turned on first
  NTFY_TOPIC         - optional for "assignment_draft" — sends a short
                       confirmation push once the email goes out (or if it
                       fails), so you know it worked without checking your
                       inbox
"""

import os
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

from check_assignments import load_state, save_state, STATE_PATH_DEFAULT, send_ntfy_notification, is_draftable


def generate_draft(api_key, title, detail, timeout=60):
    """
    Generate a genuine starting point for an assignment — structure, key
    points to hit, a rough first pass — explicitly framed as something to
    revise and personalize, not a finished submission. Turning in AI-written
    work as your own is academic dishonesty at basically every school
    regardless of the tool used; this is meant to get you unstuck, not to
    substitute for doing the assignment.
    """
    import requests

    context = detail.strip() if detail and detail.strip() else "(No description was available from Canvas — working from the title alone.)"

    prompt = (
        "A student is stuck getting started on this assignment and wants a rough "
        "first draft to work from — something to react to, restructure, and "
        "rewrite in their own words, not a finished submission. Write a genuine "
        "starting draft: an outline with real substance under each section, or "
        "for a short-answer/quiz-style assignment, your best rough attempt at "
        "the actual content. This is going in an email, so plain text formatting "
        "is fine (blank lines between sections, no markdown symbols). Keep it to "
        "roughly 500-800 words. Do not include any preamble about what you're "
        "doing — just the draft content itself. At the very end, on its own "
        "line, add a short reminder that this is a starting point to revise and "
        "personalize, not something to submit as-is.\n\n"
        f"Assignment title: {title}\n"
        f"Instructions: {context[:3000]}"
    )

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 1500,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    text_blocks = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
    return " ".join(text_blocks).strip() or None


def send_email(gmail_address, gmail_app_password, to_address, subject, body, timeout=30):
    """Send a plain-text email via Gmail's SMTP relay, using an App Password
    (not the account's real password — Gmail rejects real passwords for
    SMTP when 2-Step Verification is on, which it needs to be for App
    Passwords to exist in the first place)."""
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = to_address

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=timeout) as server:
        server.starttls()
        server.login(gmail_address, gmail_app_password)
        server.send_message(msg)


def main():
    command = os.environ.get("COMMAND", "")
    assignment_id = os.environ.get("ASSIGNMENT_ID", "")
    assignment_ids_raw = os.environ.get("ASSIGNMENT_IDS", "")
    snooze_hours = int(os.environ.get("SNOOZE_HOURS") or "2")
    state_path = os.environ.get("STATE_PATH", STATE_PATH_DEFAULT)

    if command == "assignments_bulk_done":
        ids = [i.strip() for i in assignment_ids_raw.split(",") if i.strip()]
        if not ids:
            print("ERROR: no ASSIGNMENT_IDS provided for bulk done", file=sys.stderr)
            sys.exit(1)

        state = load_state(state_path)
        assignments = state.get("assignments", {})
        marked = 0
        for aid in ids:
            record = assignments.get(aid)
            if record:
                record["status"] = "done"
                record.pop("snooze_until", None)
                marked += 1
        save_state(state_path, state)
        print(f"Bulk-marked {marked}/{len(ids)} assignment(s) done.")
        return

    if not assignment_id:
        print("ERROR: no ASSIGNMENT_ID provided", file=sys.stderr)
        sys.exit(1)

    state = load_state(state_path)
    assignment = state.get("assignments", {}).get(assignment_id)

    if not assignment:
        # Nothing to do — maybe an old button tap for something no longer tracked.
        print(f"No record for assignment {assignment_id}; nothing to update.")
        return

    if command == "assignment_done":
        assignment["status"] = "done"
        assignment.pop("snooze_until", None)
        print(f"Marked {assignment_id} ({assignment.get('summary')}) complete.")
        save_state(state_path, state)

    elif command == "assignment_snooze":
        until = datetime.now(timezone.utc) + timedelta(hours=snooze_hours)
        assignment["status"] = "snoozed"
        assignment["snooze_until"] = until.isoformat()
        print(f"Snoozed {assignment_id} ({assignment.get('summary')}) until {until.isoformat()}.")
        save_state(state_path, state)

    elif command == "assignment_draft":
        anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        gmail_address = os.environ.get("GMAIL_ADDRESS", "")
        gmail_app_password = os.environ.get("GMAIL_APP_PASSWORD", "")
        ntfy_topic = os.environ.get("NTFY_TOPIC", "")
        summary = assignment.get("summary", assignment_id)

        if not is_draftable(summary):
            print(f"Skipping draft for {assignment_id} ({summary}) — not a draftable assignment type.")
            if ntfy_topic:
                send_ntfy_notification(
                    ntfy_topic,
                    f"📝 No draft for: {summary}",
                    "This looks like a quiz/exam/survey-type item — drafting doesn't apply here.",
                )
            return

        if not anthropic_api_key or not gmail_address or not gmail_app_password:
            print(
                "ERROR: assignment_draft needs ANTHROPIC_API_KEY, GMAIL_ADDRESS, "
                "and GMAIL_APP_PASSWORD set",
                file=sys.stderr,
            )
            sys.exit(1)

        # Cache the draft so tapping again doesn't re-call the API or re-cost anything.
        if assignment.get("draft"):
            draft = assignment["draft"]
            print(f"Using cached draft for {assignment_id}.")
        else:
            try:
                draft = generate_draft(anthropic_api_key, summary, assignment.get("detail", ""))
            except Exception as exc:  # noqa: BLE001
                print(f"ERROR: draft generation failed for {assignment_id}: {exc}", file=sys.stderr)
                draft = None

            if not draft:
                if ntfy_topic:
                    send_ntfy_notification(
                        ntfy_topic,
                        f"📝 Draft failed: {summary}",
                        "Couldn't generate a draft this time — try tapping Draft again in a bit.",
                    )
                return
            assignment["draft"] = draft
            save_state(state_path, state)

        link = assignment.get("link", "")
        body = draft + (f"\n\nAssignment link: {link}" if link else "")

        try:
            send_email(gmail_address, gmail_app_password, gmail_address, f"Draft: {summary}", body)
            print(f"Emailed draft for {assignment_id} to {gmail_address}.")
            if ntfy_topic:
                send_ntfy_notification(ntfy_topic, f"📧 Draft emailed: {summary}", "Check your inbox.")
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: failed to email draft for {assignment_id}: {exc}", file=sys.stderr)
            if ntfy_topic:
                send_ntfy_notification(
                    ntfy_topic,
                    f"📝 Draft email failed: {summary}",
                    "The draft was generated but the email didn't send — check GMAIL_ADDRESS/GMAIL_APP_PASSWORD.",
                )

    else:
        print(f"WARNING: unrecognized command '{command}'", file=sys.stderr)


if __name__ == "__main__":
    main()
