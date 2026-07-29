#!/usr/bin/env python3
"""
stopbot/watcher.py

Polls the local Messages database (~/Library/Messages/chat.db) for new
incoming texts, decides whether they look like political campaign spam,
and if so, replies "STOP" automatically via AppleScript.

Requires:
  - Terminal (or whatever runs this script) to have Full Disk Access
    (System Settings > Privacy & Security > Full Disk Access)
  - Messages app automation permission the first time osascript runs
    (macOS will prompt you)

Run manually first to test:
    python3 watcher.py

Once it behaves the way you want, install it as a background job with
install_launchd.sh so it runs automatically at login.
"""

import sqlite3
import subprocess
import time
import re
import os
import shutil
import sys
import json
import fcntl
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HOME = Path.home()
CHAT_DB = HOME / "Library" / "Messages" / "chat.db"
STATE_FILE = Path(__file__).parent / "state.json"
LOCK_FILE = Path(__file__).parent / "stopbot.lock"
ALLOWLIST_FILE = Path(__file__).parent / "allowlist.txt"
CUSTOM_KEYWORDS_FILE = Path(__file__).parent / "keywords.json"
LOG_FILE = Path(__file__).parent / "stopbot.log"

POLL_INTERVAL_SECONDS = 15

# If True, log what WOULD happen but never actually send STOP.
# Run with this on for a while after any change, and check stopbot.log
# before trusting it to send real replies.
DRY_RUN = True

# Use a local Ollama model as a secondary check for ambiguous messages.
# Set to False to disable and rely on the keyword heuristic only.
USE_OLLAMA = True
OLLAMA_MODEL = "llama3.2"
OLLAMA_URL = "http://localhost:11434/api/generate"

# Messages shorter than this never go to Ollama. Real bulk spam
# (political fundraising, marketing) is long-form with links and
# calls to action; a short message carries too little signal and the
# model just guesses — which flagged a friend's literal "Test send".
# Short messages can still match via keyword.
OLLAMA_MIN_TEXT_LENGTH = 50

# How many DISTINCT keywords must appear (as whole words) before the
# keyword check fires. One political/marketing word in a real
# conversation is normal ("are you going to vote?"); actual blasts pile
# them up ("campaign" + "donate" + "election"...). Raise this if
# dry-run logs still show keyword false positives, lower to 1 to make
# any single keyword decisive again.
KEYWORD_MIN_MATCHES = 2

# If True, anyone in your Contacts app is NEVER evaluated, regardless of
# what they text — even if their message happens to contain a keyword.
# Strongly recommended: real spam/political texts come from numbers you
# haven't saved, and this avoids the whole class of false positives from
# friends/family using normal words that happen to also be keywords.
EXCLUDE_CONTACTS = True
CONTACTS_CACHE_TTL_SECONDS = 15 * 60  # how often to re-check Contacts.app

# Default keyword seed. This is only used ONCE, the very first time
# keywords.json doesn't exist yet, to populate it with sensible starting
# terms. After that, keywords.json is the single source of truth —
# every term (built-in or user-added) lives there and is equally
# editable/removable from keyword_manager.py's web UI. There's
# deliberately no separate hardcoded list living in this file anymore.
DEFAULT_KEYWORD_SEED = [
    # Political campaign / fundraising vocabulary
    "paid for by", "campaign", "donate", "contribute", "election", "vote",
    "poll", "candidate", "gop", "democrat", "republican", "fec", "actblue",
    "winred", "trump", "jacqueline", "senate", "congress", "nominee",
    "super pac",
    # General bulk-marketing / compliance-boilerplate signals — chosen
    # because real people essentially never use this phrasing, so it's
    # a low-false-positive way to catch spam broadly, not just political.
    "unsubscribe", "opt out",
    "msg & data rates may apply", "promo code", "exclusively for you", "vip",
]

_keywords_cache = {"mtime": None, "terms": []}


def get_all_keywords():
    """
    Loads the full keyword list from keywords.json — this is the single
    source of truth for everything the classifier matches on, editable
    via keyword_manager.py's web UI. Seeds the file with sensible
    defaults the first time it doesn't exist. Cached and only re-read
    when the file's modification time changes, so edits made through the
    UI while the bot is running take effect on the next poll without a
    restart.
    """
    global _keywords_cache
    if not CUSTOM_KEYWORDS_FILE.exists():
        CUSTOM_KEYWORDS_FILE.write_text(
            json.dumps({"keywords": DEFAULT_KEYWORD_SEED}, indent=2)
        )
    mtime = CUSTOM_KEYWORDS_FILE.stat().st_mtime
    if mtime != _keywords_cache["mtime"]:
        try:
            data = json.loads(CUSTOM_KEYWORDS_FILE.read_text())
            _keywords_cache = {"mtime": mtime, "terms": data.get("keywords", [])}
        except Exception as e:
            log(f"Could not read keywords.json ({e}); ignoring keywords for now.")
    return _keywords_cache["terms"]

# ---------------------------------------------------------------------------
# State handling
# ---------------------------------------------------------------------------

def load_state():
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())
        if "known_read_status" not in state:
            log("Old state.json format detected — migrating to read-status tracking.")
            state["known_read_status"] = {}
            state.pop("last_rowid", None)
        return state
    log("First run detected. Any message currently sitting unread will be "
        "evaluated right away; already-read messages are ignored.")
    return {"known_read_status": {}, "replied_senders": {}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def prune_known_read_status(state, keep_last_n=5000):
    """Keeps state.json from growing unbounded over months of use."""
    d = state["known_read_status"]
    if len(d) <= keep_last_n:
        return
    # Keep only the highest-numbered (most recent) rowids.
    keys_sorted = sorted(d.keys(), key=lambda k: int(k))
    for k in keys_sorted[:-keep_last_n]:
        del d[k]


def load_allowlist():
    """Numbers/contacts in this file are NEVER auto-replied to."""
    if not ALLOWLIST_FILE.exists():
        ALLOWLIST_FILE.write_text(
            "# One phone number or short code per line.\n"
            "# Messages from these senders will NEVER get an auto STOP reply.\n"
            "# Example:\n"
            "# +15551234567\n"
        )
        return set()
    lines = ALLOWLIST_FILE.read_text().splitlines()
    entries = {l.strip() for l in lines if l.strip() and not l.strip().startswith("#")}
    # A short-code seen sending a legitimate reservation confirmation that
    # got misclassified once — always exclude known-legit service codes.
    entries.add("36246")  # OpenTable
    return entries


_contacts_cache = {"loaded_at": 0, "numbers": set(), "emails": set()}


def normalize_phone(s):
    """Strips formatting so numbers compare equal regardless of spaces,
    dashes, parens, or a leading country code (e.g. '+1 (555) 123-4567'
    and '5551234567' both normalize to the same 10 digits)."""
    digits = re.sub(r"\D", "", s)
    return digits[-10:] if len(digits) >= 10 else digits


def ensure_contacts_running():
    """
    Launch Contacts.app if it isn't already running. The AppleScript
    contact query silently returns nothing when Contacts.app is closed
    (e.g. after a reboot), which disables contact-exclusion until someone
    notices — so launch it ourselves instead of relying on it being open.
    '-g' keeps it in the background so it never steals focus.
    """
    try:
        running = subprocess.run(
            ["pgrep", "-x", "Contacts"], capture_output=True
        ).returncode == 0
        if not running:
            subprocess.run(["open", "-g", "-a", "Contacts"], timeout=10, check=True)
            time.sleep(5)  # give it a moment to finish launching before we query it
            log("Contacts.app was not running — launched it for contact-exclusion.")
    except Exception as e:
        log(f"Could not launch Contacts.app ({e}) — contact-exclusion may be "
            f"unavailable until it is opened manually.")


def get_contact_phone_numbers_and_emails():
    """
    Returns (phone_numbers, emails) for everyone in Contacts.app — sets
    of normalized phone numbers and lowercased emails. Cached for
    CONTACTS_CACHE_TTL_SECONDS. iMessage can address someone by either
    their phone number OR their email, so both need checking; missing
    either one would let real contacts slip through as "unknown."
    Requires one-time Automation permission for Contacts (macOS will
    prompt the first time).
    """
    global _contacts_cache
    now = time.time()
    if (now - _contacts_cache["loaded_at"] < CONTACTS_CACHE_TTL_SECONDS
            and (_contacts_cache["numbers"] or _contacts_cache["emails"])):
        return _contacts_cache["numbers"], _contacts_cache["emails"]

    ensure_contacts_running()

    phones, emails = set(), set()
    try:
        phone_script = '''
        tell application "Contacts"
            set output to {}
            repeat with p in people
                repeat with ph in phones of p
                    set end of output to (value of ph)
                end repeat
            end repeat
            return output
        end tell
        '''
        email_script = '''
        tell application "Contacts"
            set output to {}
            repeat with p in people
                repeat with em in emails of p
                    set end of output to (value of em)
                end repeat
            end repeat
            return output
        end tell
        '''
        phone_result = subprocess.run(
            ["osascript", "-e", phone_script],
            capture_output=True, text=True, timeout=30, check=True,
        )
        email_result = subprocess.run(
            ["osascript", "-e", email_script],
            capture_output=True, text=True, timeout=30, check=True,
        )
        raw_numbers = [n.strip() for n in phone_result.stdout.split(",") if n.strip()]
        raw_emails = [e.strip() for e in email_result.stdout.split(",") if e.strip()]
        phones = {normalize_phone(n) for n in raw_numbers}
        phones.discard("")
        emails = {e.lower() for e in raw_emails}
        _contacts_cache = {"loaded_at": now, "numbers": phones, "emails": emails}
        log(f"Loaded {len(phones)} contact phone number(s) and {len(emails)} "
            f"contact email(s) for exclusion.")
    except Exception as e:
        log(f"Could not load Contacts ({e}) — is Automation permission granted "
            f"for Contacts? Contact-exclusion unavailable until this works.")
    return _contacts_cache["numbers"], _contacts_cache["emails"]


def is_known_contact(sender):
    """True if sender's phone number OR email matches someone in
    Contacts.app — iMessage can address someone by either, so both are
    checked to avoid real contacts slipping through as 'unknown.'"""
    phones, emails = get_contact_phone_numbers_and_emails()
    if "@" in sender:
        return sender.lower() in emails
    normalized_sender = normalize_phone(sender)
    if not normalized_sender:
        return False
    return normalized_sender in phones


def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

# ---------------------------------------------------------------------------
# Reading new messages from chat.db
# ---------------------------------------------------------------------------

def extract_text_from_attributed_body(blob):
    """
    Rich-text iMessages (bold, links, some formatting) store their content
    in the 'attributedBody' column as a binary NSAttributedString archive,
    NOT in the plain 'text' column, which ends up NULL for these messages.

    We don't need a perfect parse — just enough readable text to run our
    keyword matching and to log a readable snippet. The actual message
    body is reliably the longest contiguous run of printable characters
    in the blob, so we extract that.
    """
    if not blob:
        return None
    try:
        decoded = blob.decode("utf-8", errors="ignore")
    except Exception:
        return None
    runs = re.findall(r"[ -~\n]{15,}", decoded)
    if not runs:
        return None
    return max(runs, key=len).strip()


def get_all_incoming_messages():
    """
    Returns a list of dicts: {rowid, sender, text, is_read} for every
    INCOMING message (is_from_me = 0). Read/unread filtering and
    new-vs-already-seen logic happens in main(), by comparing is_read
    against what we saw last time — this is what lets a message that
    gets manually marked unread again correctly re-trigger.
    """
    if not CHAT_DB.exists():
        log(f"ERROR: could not find {CHAT_DB}. Is Full Disk Access granted?")
        return []

    # Use SQLite's own backup API rather than a raw file copy. chat.db is
    # in WAL mode and gets written to constantly by Messages.app; copying
    # the raw file bytes can grab an inconsistent snapshot and produce
    # "database disk image is malformed" errors. Connection.backup()
    # performs a proper, consistent online backup instead.
    tmp_copy = Path("/tmp/stopbot_chat_copy.db")
    tmp_copy.unlink(missing_ok=True)
    try:
        src_conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
        dest_conn = sqlite3.connect(str(tmp_copy))
        src_conn.backup(dest_conn)
        src_conn.close()
    except sqlite3.Error as e:
        log(f"ERROR: could not back up chat.db ({e}). Is Full Disk Access granted "
            f"to the exact python3 binary running this script?")
        return []

    conn = dest_conn
    cur = conn.cursor()

    query = """
    SELECT message.ROWID, handle.id, message.text, message.attributedBody, message.is_read
    FROM message
    LEFT JOIN handle ON message.handle_id = handle.ROWID
    WHERE message.is_from_me = 0
    ORDER BY message.ROWID ASC
    """
    cur.execute(query)
    rows = cur.fetchall()
    conn.close()
    tmp_copy.unlink(missing_ok=True)

    results = []
    for rowid, sender, text, attributed_body, is_read in rows:
        if not sender:
            continue
        if not text:
            text = extract_text_from_attributed_body(attributed_body)
        if text:
            results.append({
                "rowid": rowid, "sender": sender, "text": text,
                "is_read": is_read,
            })
    return results

# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def looks_like_spam_keyword(text):
    """
    Returns the list of keywords found in the message as WHOLE words
    (so "vote" no longer matches "devoted", "fec" no longer matches
    "perfect"). The caller fires only when at least KEYWORD_MIN_MATCHES
    distinct keywords hit — one stray political word in a real
    conversation is not a blast. Multi-word phrases ("paid for by",
    "super pac") match as whole phrases the same way.
    """
    text_lower = text.lower()
    hits = []
    for term in get_all_keywords():
        term = term.lower().strip()
        if not term:
            continue
        # (?<!\w)/(?!\w) instead of \b so phrases that start or end with
        # punctuation ("msg & data rates may apply") still anchor cleanly.
        if re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text_lower):
            hits.append(term)
    return hits


def has_bulk_marker(text):
    """
    True if the message contains a URL or a dollar amount. Bulk spam
    with a call to action essentially always carries one of these
    (donation link, promo link, dollar ask); genuine personal notes
    essentially never do. Used to gate the Ollama check so a personal
    message from an unsaved number can never be auto-flagged on the
    model's judgment alone — that exact failure sent this bot after a
    friend's "Test send". Keyword matches are not gated by this.
    """
    return bool(re.search(
        r"https?://|www\.|\b\w[\w-]*\.[a-z]{2,}/\S|\$\d", text, re.I))


def looks_like_spam_ollama(text):
    """Ask a local Ollama model to classify ambiguous messages."""
    try:
        import urllib.request

        # Few-shot with SPAM/OK labels and temperature 0. llama3.2-class
        # models are unreliable with abstract zero-shot rules (tested:
        # they answer the conservative default for everything at temp 0)
        # but track concrete examples well. The examples deliberately
        # cover the observed false-positive classes: transactional
        # notifications, codes, and personal notes from unsaved numbers.
        prompt = (
            "You classify text messages as SPAM or OK. SPAM means bulk "
            "outreach sent to many people: political fundraising or "
            "get-out-the-vote asks, mass marketing, promotional blasts. "
            "OK means everything else: deliveries, confirmations, "
            "verification codes, account alerts, and personal notes. "
            "Bulk texts often pretend to be from a specific person — "
            "judge by the content, not the sender. If unsure, answer OK.\n\n"
            "Message: It's Kamala. Tonight marks our campaign's final "
            "fundraising deadline. Can you chip in $25 before midnight?\n"
            "Answer: SPAM\n\n"
            "Message: FINAL HOURS: 50% off sitewide ends tonight! Shop "
            "now: example.com/deals\n"
            "Answer: SPAM\n\n"
            "Message: Your appointment is confirmed for Thursday at 2pm. "
            "Reply C to confirm.\n"
            "Answer: OK\n\n"
            "Message: Your Uber code is 8841. Never share this code.\n"
            "Answer: OK\n\n"
            "Message: Delta: your flight DL1223 departs on time at "
            "3:10pm from gate B7.\n"
            "Answer: OK\n\n"
            "Message: Hi, it's Sam from the book club — Rachel gave me "
            "your number. We meet Thursday if you'd like to join!\n"
            "Answer: OK\n\n"
            "Reply with only one word, SPAM or OK.\n"
            f"Message: {text}\n"
            "Answer:"
        )
        payload = json.dumps({
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            # Deterministic: the same message must always classify the
            # same way, and sampling noise was flipping borderline cases.
            "options": {"temperature": 0},
        }).encode()

        req = urllib.request.Request(
            OLLAMA_URL, data=payload,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        answer = data.get("response", "").strip().upper()
        return answer.startswith("SPAM")
    except Exception as e:
        # Log only once per run instead of once per message, to avoid
        # flooding stopbot.log when Ollama isn't running/configured.
        global _ollama_warned
        if not _ollama_warned:
            log(f"Ollama check failed ({e}). Falling back to keyword-only "
                f"matching for the rest of this run. Run 'ollama serve' and "
                f"'ollama pull {OLLAMA_MODEL}' to enable it, or set USE_OLLAMA = False.")
            _ollama_warned = True
        return None


_ollama_warned = False


def is_spam(text):
    """
    Returns (is_spam: bool, source: str) so every decision is traceable
    in the log — "keyword: <terms that hit>" or "ollama". Ollama
    connection failures are treated as NOT spam (fail-safe), never as a
    silent yes.
    """
    keyword_hits = looks_like_spam_keyword(text)
    if len(keyword_hits) >= KEYWORD_MIN_MATCHES:
        return True, "keyword: " + ", ".join(keyword_hits)
    if (USE_OLLAMA
            and len(text.strip()) >= OLLAMA_MIN_TEXT_LENGTH
            and has_bulk_marker(text)):
        result = looks_like_spam_ollama(text)
        if result is True:
            return True, "ollama"
    return False, None

# ---------------------------------------------------------------------------
# Sending the reply
# ---------------------------------------------------------------------------

def send_stop(sender):
    """
    Sends the literal text 'STOP' to sender via Messages.app, trying
    iMessage first, then falling back to explicit SMS (needed for
    numbers that can't receive iMessage — this requires Text Message
    Forwarding to be enabled on your iPhone), then a last-resort
    generic attempt.

    Note: a successful AppleScript call here means Messages.app ACCEPTED
    the send request, not that it was necessarily delivered — actual
    delivery happens asynchronously and can still fail silently
    (e.g. show "Not Delivered" later). This is a known limitation of
    scripting Messages.app; there's no reliable synchronous delivery
    confirmation available.
    """
    attempts = [
        ("SMS", f'''
        tell application "Messages"
            set targetService to id of 1st service whose service type = SMS
            set targetBuddy to buddy "{sender}" of service id targetService
            send "STOP" to targetBuddy
        end tell
        '''),
        ("iMessage", f'''
        tell application "Messages"
            set targetService to id of 1st service whose service type = iMessage
            set targetBuddy to buddy "{sender}" of service id targetService
            send "STOP" to targetBuddy
        end tell
        '''),
        ("iMessage participant fallback", f'''
        tell application "Messages"
            send "STOP" to participant "{sender}" of (service 1 whose service type is iMessage)
        end tell
        '''),
    ]
    for label, script in attempts:
        try:
            subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True)
            log(f"Sent STOP to {sender} via {label}")
            return True
        except subprocess.CalledProcessError as e:
            log(f"{label} send failed for {sender}: {e.stderr.strip()}")
    return False

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def acquire_lock():
    """
    Refuses to let a second instance of this script run at the same time
    as an existing one. Without this, e.g. a leftover manual test run in
    an old Terminal tab plus the launchd background job can BOTH poll
    and BOTH send real STOP replies independently, unaware of each
    other — leading to duplicate sends and confusing, hard-to-trace
    behavior.
    """
    lock_fp = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"ERROR: stopbot is already running (lock held on {LOCK_FILE}). "
              f"Refusing to start a second instance. If you're sure nothing "
              f"else is running, delete {LOCK_FILE} and try again.")
        sys.exit(1)
    return lock_fp  # keep a reference alive for the life of the process


def main():
    _lock_fp = acquire_lock()
    log("stopbot watcher starting up.")
    state = load_state()
    allowlist = load_allowlist()

    while True:
        allowlist = load_allowlist()  # reload each cycle so edits take effect live
        all_msgs = get_all_incoming_messages()

        for msg in all_msgs:
            rowid_key = str(msg["rowid"])
            sender = msg["sender"]
            text = msg["text"]
            is_read = msg["is_read"]

            previously_seen = rowid_key in state["known_read_status"]
            prev_is_read = state["known_read_status"].get(rowid_key)
            state["known_read_status"][rowid_key] = is_read

            became_unread = (not is_read) and (
                not previously_seen or prev_is_read == 1
            )
            if not became_unread:
                continue

            if sender in allowlist:
                continue

            if EXCLUDE_CONTACTS and is_known_contact(sender):
                continue

            spam, source = is_spam(text)
            if spam:
                log(f"Spam text detected from {sender} (matched via {source}): {text[:80]!r}")
                last_sent = state["replied_senders"].get(sender, 0)
                if time.time() - last_sent < 60:
                    log(f"Skipping send to {sender} — already sent STOP within the last minute.")
                    continue
                if DRY_RUN:
                    log(f"[DRY RUN] Would send STOP to {sender} (no message actually sent)")
                    state["replied_senders"][sender] = time.time()
                elif send_stop(sender):
                    log(f"Sent STOP to {sender}")
                    state["replied_senders"][sender] = time.time()
                else:
                    log(f"Failed to send STOP to {sender}")

        prune_known_read_status(state)
        save_state(state)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("stopbot watcher stopped by user.")
        sys.exit(0)
