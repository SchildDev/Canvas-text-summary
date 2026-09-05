#!/usr/bin/env python3
"""
Handles a single Complete/Snooze/Draft action, triggered by GitHub's
repository_dispatch API (which the tap buttons in your ntfy notifications
call directly).

Reads the event type + assignment id passed in by the workflow (as
environment variables — see .github/workflows/handle-assignment-action.yml)
and updates state.json accordingly. The workflow commits the change back
to the repo after this script runs.

Required environment variables:
  COMMAND        - "assignment_done", "assignment_snooze", or "assignment_draft"
  ASSIGNMENT_ID  - the assignment's id (matches a key in state.json's "assignments")

Optional:
  SNOOZE_HOURS       - how many hours to snooze for (default: 2; the daily
                       script also passes its own snooze_hours in the button
                       payload, but this env var is the fallback if missing).
                       Note: since notifications only actually go out on the
                       daily schedule, an item snoozed for 2 hours reappears
                       at the next scheduled run that's at least 2 hours
                       later — not necessarily exactly 2 hours from now.
  STATE_PATH         - path to the state file (default: state.json)
  NTFY_TOPIC         - required for "assignment_draft" (where to send the result)
  ANTHROPIC_API_KEY  - required for "assignment_draft"
"""

import os
import sys
from datetime import datetime, timedelta, timezone

from check_assignments import load_state, save_state, STATE_PATH_DEFAULT, send_ntfy_notification


def generate_draft(api_key, title, detail, link, timeout=60):
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
        "the actual content. Keep it to roughly 500-800 words. Do not include "
        "any preamble about what you're doing — just the draft content itself. "
        "At the very end, on its own line, add a short reminder that this is a "
        "starting point to revise and personalize, not something to submit as-is.\n\n"
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


def main():
    command = os.environ.get("COMMAND", "")
    assignment_id = os.environ.get("ASSIGNMENT_ID", "")
    snooze_hours = int(os.environ.get("SNOOZE_HOURS") or "2")
    state_path = os.environ.get("STATE_PATH", STATE_PATH_DEFAULT)

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
        ntfy_topic = os.environ.get("NTFY_TOPIC", "")
        anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not ntfy_topic or not anthropic_api_key:
            print("ERROR: assignment_draft needs NTFY_TOPIC and ANTHROPIC_API_KEY set", file=sys.stderr)
            sys.exit(1)

        # Cache the draft so tapping again doesn't re-call the API or re-cost anything.
        if assignment.get("draft"):
            draft = assignment["draft"]
            print(f"Using cached draft for {assignment_id}.")
        else:
            draft = generate_draft(
                anthropic_api_key,
                assignment.get("summary", "Untitled assignment"),
                assignment.get("detail", ""),
                assignment.get("link"),
            )
            if not draft:
                print(f"WARNING: draft generation failed for {assignment_id}", file=sys.stderr)
                send_ntfy_notification(
                    ntfy_topic,
                    f"📝 Draft failed: {assignment.get('summary', assignment_id)}",
                    "Couldn't generate a draft this time — try tapping Draft again in a bit.",
                )
                return
            assignment["draft"] = draft
            save_state(state_path, state)

        # ntfy messages have a practical size limit; keep this comfortably under it.
        message = draft if len(draft) <= 3800 else draft[:3790].rstrip() + "\n…(truncated)"
        send_ntfy_notification(
            ntfy_topic,
            f"📝 Draft: {assignment.get('summary', assignment_id)}",
            message,
            click_url=assignment.get("link"),
        )
        print(f"Sent draft for {assignment_id}.")

    else:
        print(f"WARNING: unrecognized command '{command}'", file=sys.stderr)
        return


if __name__ == "__main__":
    main()
