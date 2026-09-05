#!/usr/bin/env python3
"""
Handles a single Complete/Snooze/bulk-done action, triggered by GitHub's
repository_dispatch API (which the tap buttons in your ntfy notifications
call directly).

Reads the event type + assignment id(s) passed in by the workflow (as
environment variables — see .github/workflows/handle-assignment-action.yml)
and updates state.json accordingly. The workflow commits the change back
to the repo after this script runs.

Required environment variables:
  COMMAND         - "assignment_done", "assignment_snooze", or "assignments_bulk_done"
  ASSIGNMENT_ID   - the assignment's id, for assignment_done/assignment_snooze
  ASSIGNMENT_IDS  - comma-separated ids, for assignments_bulk_done

Optional:
  SNOOZE_HOURS   - how many hours to snooze for (default: 2; the daily
                   script also passes its own snooze_hours in the button
                   payload, but this env var is the fallback if missing).
                   Note: since notifications only actually go out on the
                   daily schedule, an item snoozed for 2 hours reappears
                   at the next scheduled run that's at least 2 hours
                   later — not necessarily exactly 2 hours from now.
  STATE_PATH     - path to the state file (default: state.json)
"""

import os
import sys
from datetime import datetime, timedelta, timezone

from check_assignments import load_state, save_state, STATE_PATH_DEFAULT


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

    elif command == "assignment_snooze":
        until = datetime.now(timezone.utc) + timedelta(hours=snooze_hours)
        assignment["status"] = "snoozed"
        assignment["snooze_until"] = until.isoformat()
        print(f"Snoozed {assignment_id} ({assignment.get('summary')}) until {until.isoformat()}.")

    else:
        print(f"WARNING: unrecognized command '{command}'", file=sys.stderr)
        return

    save_state(state_path, state)


if __name__ == "__main__":
    main()
