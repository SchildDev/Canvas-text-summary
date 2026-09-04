# Canvas Daily Assignment Text

Sends you a text every morning listing what's due on Canvas today and tomorrow —
and lets you reply to manage individual items. Runs for free on GitHub Actions
and Twilio Functions — no server to maintain.

## How it works

1. **Daily text** (GitHub Actions, on a schedule): downloads your Canvas
   calendar feed (an ICS file — public-by-link, no API token needed), figures
   out what's due, skips anything you've marked done or currently snoozed,
   summarizes each one in plain English (if `ANTHROPIC_API_KEY` is set),
   numbers the list, and texts it to you via Twilio.
2. **Replies** (Twilio Function, triggered instantly when you text back): you
   can reply `DONE 2`, `SNOOZE 2`, or `INFO 2` using the number from the text.
   The Function updates a small `state.json` file in your GitHub repo so the
   next daily text remembers.

```
DONE 2     -> stops mentioning item 2 ever again
SNOOZE 2   -> hides item 2 for a day, then it comes back if still relevant
INFO 2     -> texts back the full description + a direct link (for more than the one-liner)
OUTLINE 2  -> texts back a Claude-generated starting outline for item 2
```

A real text looks like this:

```
📚 Canvas — what's due:

Today:
1. Quiz 02 Resumes and Syllabus Quiz (GEB1030)
   Open-book quiz on resume formatting and syllabus policies.
2. Week 02 E&P (GEB1030)
   Just attend and participate in class today.
3. Hwk 2.4: Ch 12 (REE3043)

Reply DONE #, SNOOZE #, or INFO # (e.g. DONE 2)
```

(Item 3 has no summary line because that assignment had no description in
Canvas to summarize from — it still shows up, just without extra detail.)

## Setup

This has two parts: the daily texter (same as before) and the reply handler
(new). Budget about 20–25 minutes total.

### Part A — Daily text (GitHub Actions)

**1. Get your Canvas calendar feed URL**

Canvas → Calendar (left sidebar) → look in the right-hand sidebar for
**"Calendar Feed"** → copy the URL → change `webcal://` to `https://`.
That's your `CANVAS_ICS_URL`.

**2. Create a free Twilio account**

1. Sign up at https://www.twilio.com/try-twilio (free trial credit included).
2. From the Console dashboard, note your **Account SID** and **Auth Token**.
3. Get a Twilio phone number: **Phone Numbers → Buy a number**.
4. Trial accounts must verify the recipient: **Phone Numbers → Verified
   Caller IDs** → add your cell number → confirm the code it texts you.

**3. Create the GitHub repo**

Create a new repo and upload everything in this folder, keeping the folder
structure intact (`.github/workflows/`, `twilio-function/`, `state.json`,
etc.).

**4. Add GitHub Actions secrets**

Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret name       | Value                                       |
|--------------------|----------------------------------------------|
| `CANVAS_ICS_URL`  | The https:// link from step 1                 |
| `TWILIO_SID`      | Your Twilio Account SID                       |
| `TWILIO_AUTH`     | Your Twilio Auth Token                        |
| `TWILIO_FROM`     | Your Twilio phone number, e.g. `+15551234567` |
| `TWILIO_TO`       | Your cell number, e.g. `+18505551234`         |
| `ANTHROPIC_API_KEY` (optional) | Enables the plain-English one-line summaries under each item — see below |

To get an Anthropic API key: sign up at https://console.anthropic.com,
create a key under **API Keys**, and add a small amount of credit (a few
cents covers this for a whole semester — it only summarizes each assignment
once, ever, then caches it). If you skip this secret, the daily text still
works, just without the plain-English summary lines (and `INFO` replies
fall back to Canvas's raw instructor text).

**5. Allow the workflow to push commits**

Repo → **Settings → Actions → General → Workflow permissions** → select
**"Read and write permissions"** → Save. (The workflow needs this to save
`state.json` back to the repo after each run.)

**6. Test it**

**Actions** tab → **"Daily Canvas Assignment Text"** → **Run workflow**.
You should get a text within ~30 seconds. Check the run logs if not.

### Part B — Replies (Twilio Function)

**1. Create a GitHub personal access token**

GitHub → your avatar → **Settings → Developer settings → Personal access
tokens → Fine-grained tokens → Generate new token**.
- **Repository access**: only this repo
- **Permissions**: Contents → **Read and write**
- Copy the token — you won't see it again.

**2. Create the Function**

In the Twilio Console: **Functions and Assets → Services → Create Service**
(name it anything, e.g. `canvas-bot`). Inside it, **Add Function**, name it
`inbound-sms`, and paste in the contents of `twilio-function/inbound-sms.js`
from this repo.

**3. Set the Function's environment variables**

In that Service, go to **Settings → Environment Variables** and add:

| Variable        | Value                                                |
|-------------------|-------------------------------------------------------|
| `GITHUB_TOKEN`  | The fine-grained token from step 1                     |
| `GITHUB_REPO`   | `yourusername/canvas-daily-text` (your actual repo)    |
| `ALLOWED_FROM`  | Your cell number, e.g. `+18505551234` (same as `TWILIO_TO`) |
| `ANTHROPIC_API_KEY` (optional) | Same key from the GitHub Actions setup — needed for `OUTLINE` replies. Without it, `OUTLINE` just tells you it's not configured; everything else still works. |

**4. Deploy** the Service (there's a **Deploy All** button). Copy the
Function's URL (ends in `/inbound-sms`).

**5. Wire it to your phone number**

**Phone Numbers → Manage → Active numbers** → click your Twilio number →
under **Messaging**, set **"A message comes in"** to **Function** → pick
your Service and the `inbound-sms` Function → Save.

**6. Test it**

Wait for (or manually trigger) a daily text, then reply `INFO 1`. You should
get a text back with that assignment's description. Try `DONE 1` too, then
manually re-run the GitHub Action — item 1 should be gone from the list. If
you set up `ANTHROPIC_API_KEY` on the Function, try `OUTLINE 1` as well.

### Adjust the schedule (optional)

The workflow runs at **11:00 UTC daily** (7:00 AM EDT). Edit the `cron:`
line in `.github/workflows/daily-assignments.yml` — GitHub Actions cron is
always UTC. Format: `minute hour day month weekday`.

## Notes & limitations

- Canvas's ICS feed shows due dates, not submission status — that's exactly
  why `DONE`/`SNOOZE` exist: you tell the bot what to stop mentioning, since
  it can't check your actual submissions without full Canvas API access.
- Overdue, un-marked-done items keep resurfacing (bounded to the last 14
  days) rather than silently disappearing once the due date passes.
- The daily text includes a one-line Claude-generated summary under each item
  when `ANTHROPIC_API_KEY` is set. Each assignment is only summarized once,
  ever, and cached in `state.json` — it won't re-run (or re-cost anything) on
  later days. Without the key, items still show up, just without that line.
- `INFO #` replies with the full raw description + link, for when the
  one-line summary in the daily text isn't enough detail.
- `OUTLINE #` generates a short starting outline via Claude (needs
  `ANTHROPIC_API_KEY` set on the Twilio Function too, not just GitHub
  Actions — the two platforms don't share secrets). It's a starting point
  based on whatever Canvas's description field has, not a finished
  assignment — thin or generic outlines usually mean Canvas's description
  itself was sparse. Cached after the first request, so re-asking is free.
- Both the GitHub Action and the Twilio Function write to the same
  `state.json` — on the rare chance they run at the exact same moment, one
  write could be briefly overwritten by the other. Not a concern at
  personal-use volume (a few texts a day).
- Twilio trial accounts can only text/receive from **verified** numbers,
  which is exactly what you want for a personal reminder bot.
