# Canvas Daily Assignment Notifier (ntfy.sh version)

Sends you a push notification for each assignment due today/tomorrow — with
tap buttons to open it in Canvas, mark it done, or snooze it. Runs for free
on GitHub Actions. No Twilio, no phone carrier, no paid account anywhere.

## How it works

1. **Daily check** (GitHub Actions, on a schedule): downloads your Canvas
   calendar feed (an ICS file — public-by-link, no API token needed), figures
   out what's due, skips anything you've marked done or currently snoozed,
   summarizes each one in plain English (if `ANTHROPIC_API_KEY` is set), and
   sends one push notification per assignment via ntfy.sh.
2. **Tap an action** (fires instantly from your phone): each notification has
   buttons — **Open in Canvas** (goes straight to the assignment), **Done**
   (stop mentioning it), **Snooze 1d** (hide it for a day). Tapping Done or
   Snooze sends a request directly from your phone to GitHub's API, which
   triggers a second workflow to update `state.json`. No server of your own
   in between.

## One important security note

The Done/Snooze buttons need a GitHub access token to work — a tap has to be
able to authorize a change to your repo. That token travels inside the
notification through ntfy's relay server and lives on your phone once
received. **Scope it to only this one repo, nothing else.** Worst case if it
were ever exposed: someone could mess with your assignment tracker repo —
annoying, not dangerous. Setup below walks through creating a properly
scoped token.

## Setup

Budget about 15 minutes. Two parts: the ntfy app (2 minutes) and the GitHub
side (the rest).

### 1. Install ntfy and pick a topic

1. Install the **ntfy** app — [App Store](https://apps.apple.com/us/app/ntfy/id1625396347) (iOS) or [Google Play](https://play.google.com/store/apps/details?id=io.heckel.ntfy) (Android). It's free, open source, no account needed.
2. Open the app, tap **+** (subscribe to topic).
3. Make up a topic name that's long and hard to guess — anyone who knows
   this exact name could see your notifications, since it's not a private
   account. Something like `schild-canvas-x7k2p9` (random suffix, not just
   "canvas"). This is your `NTFY_TOPIC`.
4. Subscribe. You can test it immediately: go to
   `https://ntfy.sh/your-topic-name` in a browser and look for a "Send a
   test notification" option, or just wait for the real one later.

### 2. Get your Canvas calendar feed URL

Canvas → Calendar (left sidebar) → look in the right-hand sidebar for
**"Calendar Feed"** → copy the URL → change `webcal://` to `https://`.
That's your `CANVAS_ICS_URL`.

### 3. Create a scoped GitHub token

This token gets embedded in your notifications (see security note above),
so scope it tightly:

1. GitHub → your avatar (top right) → **Settings → Developer settings →
   Personal access tokens → Fine-grained tokens → Generate new token**.
2. **Repository access**: select "Only select repositories" → choose just
   this repo.
3. **Permissions**: under "Repository permissions", set **Contents** to
   **Read and write**.
4. Generate, and copy the token — you won't see it again. This is your
   `GITHUB_DISPATCH_TOKEN`.

(If GitHub's repository-dispatch API ever rejects a fine-grained token in
your testing, the fallback is a classic token — **Settings → Developer
settings → Personal access tokens → Tokens (classic) → Generate new token**,
scope: `repo`. Classic tokens are broader, so only use this if the
fine-grained one doesn't work.)

### 4. Add the GitHub Actions secrets

Repo → **Settings → Secrets and variables → Actions → New repository
secret**:

| Secret name              | Value                                              |
|----------------------------|------------------------------------------------------|
| `CANVAS_ICS_URL`         | The https:// link from step 2                        |
| `NTFY_TOPIC`             | The topic name from step 1                           |
| `GITHUB_DISPATCH_TOKEN`  | The token from step 3                                |
| `ANTHROPIC_API_KEY` (optional) | Enables plain-English summaries — see below    |

To get an Anthropic API key: sign up at https://console.anthropic.com,
create a key under **API Keys**, add a small amount of credit (a few cents
covers a whole semester — each assignment is only summarized once, ever,
then cached).

### 5. Let the workflows save their own updates

Repo → **Settings → Actions → General** → scroll to **"Workflow
permissions"** → select **"Read and write permissions"** → Save. Both
workflows need this to commit `state.json` changes back to the repo.

### 6. Test it

**Actions** tab → **"Daily Canvas Assignment Notifications"** → **Run
workflow** → **Run workflow** again to confirm. Within ~30 seconds you
should get one push notification per assignment due. Try tapping **Done**
on one — check the **Actions** tab again, you should see a new run of
**"Handle Assignment Action"** appear automatically. Manually re-run the
daily workflow afterward and confirm that assignment no longer shows up.

### Adjust the schedule (optional)

The daily workflow runs at **11:00 UTC** (7:00 AM EDT). Edit the `cron:`
line in `.github/workflows/daily-assignments.yml` — GitHub Actions cron is
always UTC. Format: `minute hour day month weekday`.

## Notes & limitations

- This is a push notification, not a phone-network text — it needs your
  phone to have wifi or cell data to arrive (true almost all the time, just
  worth knowing if you're ever somewhere with zero connectivity).
- ntfy's public server isn't end-to-end encrypted and your topic name is the
  only thing gating who can see your notifications — pick something long
  and non-obvious, and don't put anything truly sensitive through it.
- Canvas's ICS feed shows due dates, not submission status — that's exactly
  why Done/Snooze exist: you tell it what to stop mentioning, since it can't
  check your actual submissions without full Canvas API access.
- Overdue, un-marked-done items keep resurfacing (bounded to the last 14
  days) rather than silently disappearing once the due date passes.
- The daily notification includes a one-line Claude-generated summary when
  `ANTHROPIC_API_KEY` is set. Each assignment is only summarized once, ever,
  and cached in `state.json`.
