# stopbot

Watches your Mac's local Messages database for spam texts — political
campaign/fundraising texts and general bulk marketing — and auto-replies
"STOP" so you get legally opted out (TCPA/CTIA rules require both political
and commercial bulk texters to honor STOP replies).

Everything runs locally on your Mac. Nothing is sent to any external server
except, optionally, to your own local Ollama instance for a smarter
second-opinion classification pass.

## Files

> Files marked **(gitignored)** below contain your personal data —
> phone numbers, message snippets, or keyword edits — and are excluded
> from version control via `.gitignore`. They're auto-created the first
> time you run anything, so you don't need to create them yourself.

- **`watcher.py`** — the main background script. Polls
  `~/Library/Messages/chat.db` every 15 seconds, figures out which
  messages are newly unread, classifies them, and sends STOP via
  AppleScript.
- **`check_now.py`** — a manual, on-demand checker. Scans whatever is
  currently unread right now and tells you what it thinks, independent of
  `watcher.py`'s internal state — useful for testing a specific message
  without waiting for the poll loop or worrying about state weirdness.
  Run with `--send` to actually send STOP, or `--all` to check every
  message regardless of read status.
- **`keyword_manager.py`** — a tiny local web UI (`http://localhost:8765`)
  for viewing/adding/removing the keywords stopbot matches on. No
  restart needed — `watcher.py` picks up changes within one poll cycle.
- **`keywords.json`** (gitignored) — auto-created and seeded with
  sensible defaults the first time anything runs. This is the **single
  source of truth** for every keyword/phrase stopbot matches on
  (political vocabulary, bulk-marketing compliance phrases, and
  anything you add) — there's no separate hardcoded list anywhere else
  in the code.
- **`allowlist.txt`** (gitignored) — auto-created on first run. Add any
  phone numbers here that should NEVER get an auto-reply (e.g. a
  delivery service, a business you actually use, a number you've seen
  trigger a false positive).
- **`state.json`** (gitignored) — auto-created. Tracks each message's
  last-known read/unread status so the bot only fires on new arrivals or
  a message going from read → unread again (e.g. you manually re-marking
  one for testing), not on every poll cycle for something already
  handled.
- **`com.user.stopbot.plist`** / **`install_launchd.sh`** — sets this up
  as a background job (`launchd`) that starts automatically at login.
- **`stopbot.log`** / **`stopbot.out.log`** / **`stopbot.err.log`**
  (gitignored) — logs. `stopbot.log` is the readable one; the
  `.out`/`.err` files only matter if the background job is crashing
  silently (see troubleshooting below).

## How detection works

Two independent signals, either one is enough to flag a message:

1. **Keyword match** — a case-insensitive substring check against every
   entry in `keywords.json`. Fast, has no dependencies, and covers the
   large majority of real-world political and marketing spam, since both
   categories are legally required to include some form of opt-out or
   disclosure language.
2. **Local LLM check (optional)** — if the keyword check finds nothing
   and Ollama is running, the message text is sent to your local model
   and it's asked whether this looks like bulk spam. If Ollama isn't
   running/reachable, this silently and safely falls back to "not spam"
   rather than guessing — a broken Ollama connection can never cause a
   false positive.

A message only gets evaluated the moment it's *unread* — either because
it just arrived, or because you (or something) marked an already-read
message unread again. Messages that are already read are never touched.

## One-time setup

### 1. Grant Full Disk Access — to the EXACT python3 binary

This is the single most common thing that breaks after a reboot, so
read this part carefully.

`chat.db` is protected by macOS. If you run `watcher.py` by hand from
Terminal, it works because **Terminal** has Full Disk Access and
whatever it launches inherits that. But the background `launchd` job
does **not** go through Terminal — it launches Python directly — so it
needs its **own** grant, on the exact binary it uses.

Find out which python3 that is:

```bash
which python3
```

Then: System Settings → Privacy & Security → Full Disk Access → click
**+** → press **Cmd+Shift+G** → paste that exact path → Open. Make sure
the toggle next to it is on.

`install_launchd.sh` (see step 5) automatically detects and prints this
path so you can confirm it matches what you just granted access to.

### 2. Allow Messages automation

The first time the script tries to send a message via AppleScript,
macOS will pop up a permission prompt asking to allow Terminal/python3
to control Messages. Click **OK**. If you miss it, grant it manually
under System Settings → Privacy & Security → Automation.

### 3. (Optional) Install Ollama for smarter classification

```bash
brew install ollama
brew services start ollama   # runs it in the background permanently
ollama pull llama3.2
```

If you'd rather skip this entirely, open `watcher.py` and set:

```python
USE_OLLAMA = False
```

The keyword filter alone still catches the large majority of spam.

### 4. Test it manually first — with DRY_RUN on

`watcher.py` defaults to `DRY_RUN = True`, meaning it logs what it
*would* do without actually sending anything. Leave it this way for your
first run or two:

```bash
cd ~/stopbot
python3 watcher.py
```

Watch `stopbot.log`. You should see lines like:

```
Political text detected from +1... (matched via keyword): '...'
[DRY RUN] Would send STOP to +1... (no message actually sent)
```

Once you're confident it's catching the right things (and not catching
the wrong things — check for false positives!), open `watcher.py` and
set `DRY_RUN = False`.

Press Ctrl+C to stop the test run.

### 5. Install as a background job

```bash
chmod +x install_launchd.sh
./install_launchd.sh
```

This detects your python3 path, fills in the plist template, copies it
into `~/Library/LaunchAgents/`, and starts it. It'll now also start
automatically every time you log in — **provided** Full Disk Access is
granted to that same python3 path (step 1).

To stop/uninstall:

```bash
launchctl unload ~/Library/LaunchAgents/com.user.stopbot.plist
rm ~/Library/LaunchAgents/com.user.stopbot.plist
```

To reload after editing `watcher.py` or any config:

```bash
launchctl unload ~/Library/LaunchAgents/com.user.stopbot.plist
launchctl load ~/Library/LaunchAgents/com.user.stopbot.plist
```

## Managing keywords

```bash
python3 keyword_manager.py
```

Opens a page at `http://localhost:8765` showing every keyword as a
removable pill. Add a word/phrase in the box, or click the × to remove
one. Changes take effect for `watcher.py` within one poll cycle — no
restart needed. This is just an editing tool for `keywords.json`; it
doesn't need to stay running.

## Manual/on-demand testing

```bash
python3 check_now.py            # dry run — shows what's currently unread + verdict
python3 check_now.py --send     # actually sends STOP to anything flagged
python3 check_now.py --all      # check every message, not just unread ones
```

Handy for testing a specific message right now without waiting on the
background loop, or for debugging why something wasn't caught.

## Tuning

- **False positives (real contacts/messages getting a STOP reply):**
  add the sender's number to `allowlist.txt` (one per line), or remove
  the specific keyword that tripped it via `keyword_manager.py`. Be
  aware some keywords (e.g. "vote," "poll," "congress") have legitimate
  everyday uses in casual conversation — this is a real tradeoff of
  keyword-based detection, not a bug.
- **False negatives (spam not caught):** open `keyword_manager.py` and
  add the missing word/phrase, or turn on `USE_OLLAMA` for a smarter
  second check on ambiguous messages.
- **Poll frequency:** change `POLL_INTERVAL_SECONDS` in `watcher.py`
  (default 15 seconds).

## Limitations / things to know

- **This only sees messages that land in Messages.app on your Mac**
  (iMessage, and SMS if Text Message Forwarding is enabled from your
  iPhone: Settings → Messages → Text Message Forwarding).
- **Sending isn't guaranteed to actually deliver.** The script tries SMS
  first, then iMessage as fallback, since many bulk-texting long codes
  can't receive iMessage at all. But AppleScript's `send` only confirms
  Messages.app *accepted* the request, not that it was delivered —
  actual delivery happens asynchronously and can still fail silently
  (same as if you'd typed and sent it by hand). There's no reliable way
  to get synchronous delivery confirmation from Messages.app scripting.
- **It replies using your own Mac's Messages account** — same as if you
  typed it yourself.
- **Rich-text messages** (bold, italic, some links) get their content
  extracted from a binary blob (`attributedBody`) rather than the plain
  `text` column, since macOS stores them differently. This extraction is
  a best-effort heuristic (longest readable text run in the blob), not a
  perfect parse, but works well in practice for classification purposes.
- **A "successful" classification is not a legal guarantee of opt-out**
  — if a sender ignores STOP replies entirely (some no-reply-capable
  long codes exist), no amount of retrying will fix that.
- This is a personal automation script, not an App Store app — you're
  running it yourself, so you're in full control, but that also means
  you're responsible for how it behaves. Double-check the allowlist and
  keyword list so it doesn't reply to friends, family, or businesses you
  actually use.
