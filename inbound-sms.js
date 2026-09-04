/**
 * Twilio Function: handles replies to the daily Canvas assignment text.
 *
 * Deploy this in the Twilio Console under Functions & Assets, then set it
 * as the "A message comes in" webhook for your Twilio phone number
 * (Phone Numbers -> Manage -> Active Numbers -> click your number).
 *
 * Supported replies (case-insensitive), using the number from the daily text:
 *   DONE 2     - mark item 2 done, it stops appearing
 *   SNOOZE 2   - hide item 2 for SNOOZE_DAYS (default 1), then it reappears
 *   INFO 2     - reply with the assignment's description + direct link
 *   OUTLINE 2  - reply with a Claude-generated starting outline for item 2
 *
 * Required Function environment variables (set in Console -> Functions ->
 * Configure -> Environment Variables):
 *   GITHUB_TOKEN   - fine-grained personal access token, scoped ONLY to
 *                    this repo, with "Contents: Read and write" permission
 *   GITHUB_REPO    - "yourusername/canvas-daily-text"
 *   ALLOWED_FROM   - your own phone number, e.g. +18505551234 (same as the
 *                    TWILIO_TO secret in GitHub Actions). Prevents anyone
 *                    else who texts your Twilio number from controlling it.
 *
 * Optional:
 *   STATE_PATH         - path to the state file in the repo (default: state.json)
 *   SNOOZE_DAYS        - how many days SNOOZE hides an item for (default: 1)
 *   ANTHROPIC_API_KEY  - required for OUTLINE; same key you used for the
 *                        GitHub Actions secret, added here separately since
 *                        the two platforms don't share environment variables.
 */

const https = require('https');

function jsonRequest(hostname, path, method, headers, body) {
  return new Promise((resolve, reject) => {
    const payload = body ? JSON.stringify(body) : null;
    const options = { hostname, path, method, headers };

    const req = https.request(options, (res) => {
      let raw = '';
      res.on('data', (chunk) => {
        raw += chunk;
      });
      res.on('end', () => {
        if (res.statusCode >= 200 && res.statusCode < 300) {
          resolve(raw ? JSON.parse(raw) : null);
        } else {
          reject(new Error(`${method} ${hostname}${path} -> ${res.statusCode}: ${raw}`));
        }
      });
    });

    req.on('error', reject);
    if (payload) req.write(payload);
    req.end();
  });
}

function githubRequest(context, method, path, body) {
  return jsonRequest(
    'api.github.com',
    `/repos/${context.GITHUB_REPO}/contents/${path}`,
    method,
    {
      'User-Agent': 'canvas-daily-text-bot',
      Authorization: `token ${context.GITHUB_TOKEN}`,
      Accept: 'application/vnd.github+json',
      'Content-Type': 'application/json',
    },
    body
  );
}

async function loadState(context, statePath) {
  const fileResp = await githubRequest(context, 'GET', statePath);
  const decoded = Buffer.from(fileResp.content, 'base64').toString('utf-8');
  return { state: JSON.parse(decoded), sha: fileResp.sha };
}

async function saveState(context, statePath, state, sha, commitMessage) {
  const encoded = Buffer.from(JSON.stringify(state, null, 2)).toString('base64');
  await githubRequest(context, 'PUT', statePath, {
    message: commitMessage,
    content: encoded,
    sha,
  });
}

async function generateOutline(apiKey, title, detail) {
  const hasDetail = detail && detail.trim().length > 0;
  const prompt = hasDetail
    ? `A student needs a starting outline for this assignment, to text to their phone (keep it under 900 characters, plain text, no markdown symbols, short bullet lines with a dash). Base it on the actual instructions below, not a generic template.\n\nTitle: ${title}\nInstructions: ${detail.slice(0, 2000)}`
    : `A student needs a generic starting outline for an assignment titled "${title}", but no instructions are available. Give a brief, sensible generic structure for this type of assignment (under 700 characters, plain text, dash bullets), and note at the end that it's generic since no description was found.`;

  const data = await jsonRequest(
    'api.anthropic.com',
    '/v1/messages',
    'POST',
    {
      'x-api-key': apiKey,
      'anthropic-version': '2023-06-01',
      'content-type': 'application/json',
    },
    {
      model: 'claude-haiku-4-5-20251001',
      max_tokens: 400,
      messages: [{ role: 'user', content: prompt }],
    }
  );

  const textBlocks = (data.content || []).filter((b) => b.type === 'text').map((b) => b.text);
  const outline = textBlocks.join(' ').trim();
  return outline || null;
}

exports.handler = async function (context, event, callback) {
  const twiml = new Twilio.twiml.MessagingResponse();

  try {
    const from = event.From;
    if (context.ALLOWED_FROM && from !== context.ALLOWED_FROM) {
      // Don't help a stranger control your bot; don't confirm anything either.
      twiml.message("This number isn't set up to control this bot.");
      return callback(null, twiml);
    }

    const body = (event.Body || '').trim();
    const match = body.match(/^(done|snooze|info|outline)\s+(\d+)\s*$/i);
    if (!match) {
      twiml.message(
        'Reply like "DONE 2", "SNOOZE 2", "INFO 2", or "OUTLINE 2" using the number from your daily text.'
      );
      return callback(null, twiml);
    }

    const command = match[1].toLowerCase();
    const number = match[2];
    const statePath = context.STATE_PATH || 'state.json';

    const { state, sha } = await loadState(context, statePath);
    const assignmentId = state.last_sent && state.last_sent[number];
    const assignment = assignmentId && state.assignments && state.assignments[assignmentId];

    if (!assignment) {
      twiml.message(
        `I don't have an item #${number} from today's list. It may be from an older text — wait for tomorrow's.`
      );
      return callback(null, twiml);
    }

    if (command === 'info') {
      const detail = assignment.ai_summary || assignment.detail || 'No extra detail available for this one.';
      const link = assignment.link || '';
      twiml.message(`${assignment.summary}\n${detail}${link ? '\n' + link : ''}`);
      return callback(null, twiml);
    }

    if (command === 'done') {
      assignment.status = 'done';
      delete assignment.snooze_until;
      await saveState(context, statePath, state, sha, `Mark done via SMS: #${number}`);
      twiml.message(`Marked "${assignment.summary}" done. Won't text you about it again.`);
      return callback(null, twiml);
    }

    if (command === 'snooze') {
      const days = parseInt(context.SNOOZE_DAYS || '1', 10);
      const until = new Date();
      until.setDate(until.getDate() + days);
      assignment.status = 'snoozed';
      assignment.snooze_until = until.toISOString().slice(0, 10);
      await saveState(context, statePath, state, sha, `Snooze via SMS: #${number}`);
      twiml.message(
        `Snoozed "${assignment.summary}" until ${assignment.snooze_until}. It'll come back after that if still relevant.`
      );
      return callback(null, twiml);
    }

    if (command === 'outline') {
      if (!context.ANTHROPIC_API_KEY) {
        twiml.message('Outlines need an ANTHROPIC_API_KEY set on this Function — see the README.');
        return callback(null, twiml);
      }

      // Cache the outline so re-asking doesn't re-call the API or re-cost anything.
      if (assignment.outline) {
        twiml.message(`${assignment.summary}\n${assignment.outline}`);
        return callback(null, twiml);
      }

      const outline = await generateOutline(context.ANTHROPIC_API_KEY, assignment.summary, assignment.detail || '');
      if (!outline) {
        twiml.message("Couldn't generate an outline for that one — try again in a bit.");
        return callback(null, twiml);
      }

      assignment.outline = outline;
      await saveState(context, statePath, state, sha, `Generate outline via SMS: #${number}`);
      twiml.message(`${assignment.summary}\n${outline}`);
      return callback(null, twiml);
    }

    return callback(null, twiml);
  } catch (err) {
    console.error(err);
    twiml.message("Something went wrong with that — I'll try again next time you text.");
    return callback(null, twiml);
  }
};

