#!/usr/bin/env python3
"""
Handles a single Done/Snooze action, triggered by GitHub's repository_dispatch
API (which the tap buttons in your ntfy notifications call directly).

Reads the event type + assignment id passed in by the workflow (as
environment variables — see .github/workflows/handle-assignment-action.yml)
and updates state.json accordingly. The workflow commits the change back
to the repo after this script runs.

Required environment variables:
  COMMAND        - "assignment_done" or "assignment_snooze"
  ASSIGNMENT_ID  - the assignment's id (matches a key in state.json's "assignments")

Optional:
  SNOOZE_DAYS    - how many days to snooze for (default: 1; the daily script
                   also passes its own snooze_days in the button payload,
                   but this env var is the fallback if that's missing)
  STATE_PATH     - path to the state file (default: state.json)
"""

import os
import sys
from datetime import date, timedelta

from check_assignments import load_state, save_state, STATE_PATH_DEFAULT


def main():
    command = os.environ.get("COMMAND", "")
    assignment_id = os.environ.get("ASSIGNMENT_ID", "")
    snooze_days = int(os.environ.get("SNOOZE_DAYS") or "1")
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
        print(f"Marked {assignment_id} ({assignment.get('summary')}) done.")

    elif command == "assignment_snooze":
        until = date.today() + timedelta(days=snooze_days)
        assignment["status"] = "snoozed"
        assignment["snooze_until"] = until.isoformat()
        print(f"Snoozed {assignment_id} ({assignment.get('summary')}) until {until.isoformat()}.")

    else:
        print(f"WARNING: unrecognized command '{command}'", file=sys.stderr)
        return

    save_state(state_path, state)


if __name__ == "__main__":
    main()
