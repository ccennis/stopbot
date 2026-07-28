#!/usr/bin/env python3
"""
stopbot/keyword_manager.py

A tiny local web page for adding/removing custom spam-detection keywords
without touching any code. Uses only Python's standard library — no
Flask or other dependencies needed.

watcher.py and check_now.py both pick up changes automatically (they
check the file's modification time every time they classify a message),
so you don't need to restart the background job after adding a word —
just give it one more poll cycle (~15 seconds).

Usage:
    python3 keyword_manager.py

This opens http://localhost:8765 in your default browser. Press Ctrl+C
in the terminal when you're done to stop the server (the keywords file
itself is unaffected — it's just a page for editing it).
"""

import json
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import unquote

KEYWORDS_FILE = Path(__file__).parent / "keywords.json"
PORT = 8765

DEFAULT_KEYWORD_SEED = [
    "paid for by", "campaign", "donate", "contribute", "election", "vote",
    "poll", "candidate", "gop", "democrat", "republican", "fec", "actblue",
    "winred", "trump", "jacqueline", "senate", "congress", "nominee",
    "super pac", "reply stop", "text stop", "unsubscribe", "opt out",
    "msg & data rates may apply", "promo code", "exclusively for you", "vip",
]


def load_keywords():
    if not KEYWORDS_FILE.exists():
        save_keywords(DEFAULT_KEYWORD_SEED)
        return list(DEFAULT_KEYWORD_SEED)
    try:
        return json.loads(KEYWORDS_FILE.read_text()).get("keywords", [])
    except Exception:
        return []


def save_keywords(keywords):
    KEYWORDS_FILE.write_text(json.dumps({"keywords": keywords}, indent=2))


PAGE_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>stopbot — custom keywords</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, sans-serif;
         max-width: 560px; margin: 40px auto; padding: 0 20px; color: #222; }
  h1 { font-size: 20px; margin-bottom: 4px; }
  p.sub { color: #666; font-size: 14px; margin-top: 0; }
  form { display: flex; gap: 8px; margin: 20px 0; }
  input[type=text] { flex: 1; padding: 10px; font-size: 14px;
                      border: 1px solid #ccc; border-radius: 6px; }
  button.add { padding: 10px 16px; font-size: 14px; cursor: pointer;
           border: none; border-radius: 6px; background: #007aff; color: white; }
  button.add:hover { background: #0060d0; }
  #list { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; padding: 0; list-style: none; }
  .chip { display: flex; align-items: center; gap: 6px;
          background: #f0f0f2; border-radius: 999px; padding: 6px 8px 6px 14px;
          font-size: 14px; }
  .chip-x { background: none; border: none; cursor: pointer; color: #888;
            font-size: 16px; line-height: 1; padding: 2px 6px; border-radius: 50%; }
  .chip-x:hover { background: #ddd; color: #c00; }
  .empty { color: #888; font-style: italic; padding: 10px 0; }
</style>
</head>
<body>
  <h1>stopbot — keywords</h1>
  <p class="sub">Any unread message containing one of these words or phrases
  (case-insensitive) gets flagged as spam. This is the full list stopbot
  checks against — nothing is hidden or hardcoded elsewhere.</p>
  <form id="addForm">
    <input type="text" id="newTerm" placeholder="e.g. sweepstakes, extended warranty" autofocus>
    <button class="add" type="submit">Add</button>
  </form>
  <ul id="list"></ul>

<script>
async function refresh() {
  const res = await fetch('/api/keywords');
  const data = await res.json();
  const list = document.getElementById('list');
  list.innerHTML = '';
  if (data.length === 0) {
    list.innerHTML = '<li class="empty">No custom keywords yet.</li>';
    return;
  }
  data.forEach(term => {
    const li = document.createElement('li');
    li.className = 'chip';
    const span = document.createElement('span');
    span.textContent = term;
    const x = document.createElement('button');
    x.textContent = '×';
    x.className = 'chip-x';
    x.title = 'Remove';
    x.onclick = async () => {
      await fetch('/api/keywords/' + encodeURIComponent(term), { method: 'DELETE' });
      refresh();
    };
    li.appendChild(span);
    li.appendChild(x);
    list.appendChild(li);
  });
}

document.getElementById('addForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const input = document.getElementById('newTerm');
  const term = input.value.trim();
  if (!term) return;
  await fetch('/api/keywords', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({term})
  });
  input.value = '';
  refresh();
});

refresh();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            body = PAGE_TEMPLATE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/keywords":
            self._send_json(load_keywords())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/api/keywords":
            length = int(self.headers.get("Content-Length", 0))
            try:
                data = json.loads(self.rfile.read(length))
            except Exception:
                data = {}
            term = (data.get("term") or "").strip()
            keywords = load_keywords()
            if term and term.lower() not in [k.lower() for k in keywords]:
                keywords.append(term)
                save_keywords(keywords)
            self._send_json(keywords)
        else:
            self.send_response(404)
            self.end_headers()

    def do_DELETE(self):
        if self.path.startswith("/api/keywords/"):
            term = unquote(self.path.split("/api/keywords/", 1)[1])
            keywords = load_keywords()
            keywords = [k for k in keywords if k.lower() != term.lower()]
            save_keywords(keywords)
            self._send_json(keywords)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # keep the terminal quiet


def main():
    server = HTTPServer(("localhost", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"Keyword manager running at {url}")
    print("Press Ctrl+C here to stop it (this only stops the web page, "
          "not the stopbot background job).")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
