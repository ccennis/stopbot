#!/usr/bin/env python3
"""
stopbot/check_now.py

Manual, on-demand check. Scans whatever is CURRENTLY unread right now,
shows you what the classifier thinks of each one, and — if you pass
--send — actually sends STOP to anything flagged as spam immediately.

This deliberately ignores state.json / the read-transition tracking the
background watcher.py loop uses, so it's useful for testing a specific
message right now without worrying about whether it was already "seen"
by a previous run.

Usage:
    python3 check_now.py            # dry run — just prints what it finds
    python3 check_now.py --send     # actually sends STOP to matches
    python3 check_now.py --all      # check ALL messages, not just unread ones
"""

import sys
from watcher import (
    get_all_incoming_messages, is_spam, send_stop, load_allowlist, log,
    is_known_contact, EXCLUDE_CONTACTS,
)


def main():
    send = "--send" in sys.argv
    check_all = "--all" in sys.argv

    allowlist = load_allowlist()
    msgs = get_all_incoming_messages()

    if not check_all:
        msgs = [m for m in msgs if not m["is_read"]]

    if not msgs:
        print("No unread messages found right now. (Use --all to check "
              "every incoming message regardless of read status.)")
        return

    print(f"Checking {len(msgs)} message(s)"
          f"{' (send mode ON)' if send else ' (dry run — nothing will be sent)'}...\n")

    for m in msgs:
        sender = m["sender"]
        text = m["text"]

        if sender in allowlist:
            print(f"[SKIP - allowlisted]  {sender}: {text[:60]!r}")
            continue

        if EXCLUDE_CONTACTS and is_known_contact(sender):
            print(f"[SKIP - known contact]  {sender}: {text[:60]!r}")
            continue

        spam, source = is_spam(text)
        if spam:
            print(f"[SPAM via {source}]  {sender}: {text[:60]!r}")
            if send:
                if send_stop(sender):
                    print(f"    -> Sent STOP to {sender}")
                else:
                    print(f"    -> FAILED to send STOP to {sender} (see stopbot.log)")
        else:
            print(f"[not spam]  {sender}: {text[:60]!r}")


if __name__ == "__main__":
    main()
