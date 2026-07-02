import argparse
import json
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import scraper

# ── SSE live-update machinery ─────────────────────────────────────────────────
_sse_clients: list = []
_sse_lock = threading.Lock()


def _watch_matches_file():
    """Background thread: push 'update' to all SSE clients when matches.json changes."""
    last_mtime = 0.0
    while True:
        try:
            mtime = scraper.MATCHES_STORE_PATH.stat().st_mtime
        except OSError:
            mtime = 0.0
        if mtime != last_mtime:
            if last_mtime != 0.0:  # skip the very first read (not a real change)
                with _sse_lock:
                    dead = []
                    for wfile in list(_sse_clients):
                        try:
                            wfile.write(b"data: update\n\n")
                            wfile.flush()
                        except OSError:
                            dead.append(wfile)
                    for wfile in dead:
                        _sse_clients.remove(wfile)
            last_mtime = mtime
        time.sleep(2)

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Job matches</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 2rem; background: #111; color: #ddd; }
  h1 { font-size: 1.3rem; }
  .tabs { margin-bottom: 1rem; }
  .tab { background: #222; color: #ccc; border: 1px solid #444; padding: 0.4rem 0.8rem;
         margin-right: 0.4rem; border-radius: 6px; cursor: pointer; font-size: 0.9rem; }
  .tab.active { background: #3a6df0; color: #fff; border-color: #3a6df0; }
  table { width: 100%; border-collapse: collapse; }
  td, th { padding: 0.5rem 0.6rem; border-bottom: 1px solid #333; text-align: left; vertical-align: top; }
  th { color: #999; font-size: 0.8rem; text-transform: uppercase; }
  a { color: #7fb0ff; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .badge { background: #b9892b; color: #111; font-size: 0.7rem; padding: 0.1rem 0.4rem;
           border-radius: 4px; margin-left: 0.4rem; }
  .badge.b2b { background: #2a9d4a; color: #fff; }
  .badge.flag { background: #b03030; color: #fff; }
  .badge.new { background: #e0b400; color: #111; font-weight: bold; }
  .score { font-weight: bold; color: #7fb0ff; }
  .source { color: #999; font-size: 0.75rem; text-transform: uppercase; }
  .stored { color: #888; font-size: 0.8rem; white-space: nowrap; }
  .skills { color: #999; font-size: 0.85rem; }
  .meta { color: #888; font-size: 0.75rem; margin-top: 0.3rem; }
  .clicks { color: #888; font-size: 0.75rem; margin-left: 0.4rem; }
  .notes-input { width: 100%; background: #1a1a1a; color: #ddd; border: 1px solid #333;
                 border-radius: 4px; padding: 0.3rem 0.4rem; font-size: 0.85rem; }
  .actions button { background: #222; color: #ccc; border: 1px solid #444; padding: 0.3rem 0.6rem;
                     margin-right: 0.3rem; border-radius: 6px; cursor: pointer; font-size: 0.8rem; }
  .actions button.active { color: #fff; border-color: transparent; }
  .actions button[data-status="interesting"].active { background: #2a9d4a; }
  .actions button[data-status="cv_sent"].active { background: #3a6df0; }
  .actions button[data-status="not_for_me"].active { background: #b03030; }
  .row.status-not_for_me { opacity: 0.45; }
</style>
</head>
<body>
<h1>Job matches</h1>
<div class="tabs" id="tabs"></div>
<table>
  <thead>
    <tr><th>Score</th><th>Offer</th><th>Source</th><th>Company</th><th>Where</th><th>Skills</th><th>Stored</th><th>Notes</th><th>Status</th></tr>
  </thead>
  <tbody id="rows"></tbody>
</table>
<script>
let allMatches = {};
let currentFilter = 'all';
const FILTERS = ['all', 'new', 'interesting', 'cv_sent', 'not_for_me'];
const LABELS = {all: 'All', new: 'New', interesting: 'Interesting', cv_sent: 'CV sent', not_for_me: 'Not for me'};

// URLs already shown to the user in a previous visit/poll -- anything not
// in this set when rendered gets a "NEW" badge. The set itself is frozen
// for the lifetime of this page load so the badge doesn't flicker away on
// the next poll; it's only refreshed (in localStorage) for the *next* visit.
const seenUrls = new Set(JSON.parse(localStorage.getItem('seenOfferUrls') || '[]'));

function escapeHtml(s) {
  return (s || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function formatStored(iso) {
  if (!iso) return '-';
  return iso.replace('T', ' ').slice(0, 16);
}

async function load() {
  const res = await fetch('/api/matches');
  allMatches = await res.json();
  localStorage.setItem('seenOfferUrls', JSON.stringify(
    [...new Set([...seenUrls, ...Object.keys(allMatches)])]
  ));
  render();
}

async function poll() {
  const res = await fetch('/api/matches');
  allMatches = await res.json();
  localStorage.setItem('seenOfferUrls', JSON.stringify(
    [...new Set([...seenUrls, ...Object.keys(allMatches)])]
  ));
  if (document.activeElement && document.activeElement.matches('.notes-input')) {
    return; // don't blow away an in-progress note edit
  }
  render();
}

function render() {
  const counts = {all: 0};
  for (const f of FILTERS) counts[f] = 0;
  for (const m of Object.values(allMatches)) {
    counts.all++;
    counts[m.status] = (counts[m.status] || 0) + 1;
  }

  document.getElementById('tabs').innerHTML = FILTERS.map(f =>
    `<button class="tab ${f === currentFilter ? 'active' : ''}" data-filter="${f}">${LABELS[f]} (${counts[f] || 0})</button>`
  ).join('');

  const entries = Object.entries(allMatches)
    .filter(([url, m]) => currentFilter === 'all' || m.status === currentFilter)
    .sort((a, b) => (b[1].score || 0) - (a[1].score || 0));

  document.getElementById('rows').innerHTML = entries.map(([url, m]) => `
    <tr class="row status-${m.status}">
      <td class="score">${m.score ?? '-'}</td>
      <td>
        ${!seenUrls.has(url) ? '<span class="badge new">NEW</span>' : ''}<a class="offer-link" href="${url}" target="_blank" rel="noopener">${escapeHtml(m.title)}</a>${m.click_count ? `<span class="clicks">opened ${m.click_count}×</span>` : ''}${m.target_employer ? '<span class="badge">target</span>' : ''}${m.b2b ? '<span class="badge b2b">B2B</span>' : ''}${(m.flags || []).map(f => `<span class="badge flag">${escapeHtml(f)}</span>`).join('')}${m.also_in ? `<span class="badge">+${m.also_in.length} more</span>` : ''}
      </td>
      <td class="source">${escapeHtml(m.source || '')}</td>
      <td>${escapeHtml(m.company)}</td>
      <td>${escapeHtml(m.where)}</td>
      <td class="skills">${(m.skills || []).join(', ')}</td>
      <td class="stored">${formatStored(m.first_seen)}</td>
      <td><input class="notes-input" data-url="${encodeURIComponent(url)}" value="${escapeHtml(m.notes || '')}" placeholder="Add a note..."></td>
      <td class="actions">
        ${['interesting', 'cv_sent', 'not_for_me'].map(s =>
          `<button data-url="${encodeURIComponent(url)}" data-status="${s}" class="${m.status === s ? 'active' : ''}">${LABELS[s]}</button>`
        ).join('')}
        ${m.cv_sent_at ? `<div class="meta">CV sent: ${m.cv_sent_at.replace('T', ' ')}</div>` : ''}
      </td>
    </tr>
  `).join('');
}

document.addEventListener('click', async (e) => {
  if (e.target.matches('.tab')) {
    currentFilter = e.target.dataset.filter;
    render();
  } else if (e.target.matches('.actions button')) {
    const url = decodeURIComponent(e.target.dataset.url);
    const clicked = e.target.dataset.status;
    const newStatus = allMatches[url].status === clicked ? 'new' : clicked;
    const res = await fetch('/api/status', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url, status: newStatus}),
    });
    const body = await res.json();
    allMatches[url].status = newStatus;
    allMatches[url].cv_sent_at = body.cv_sent_at;
    render();
  } else if (e.target.matches('.offer-link')) {
    const url = e.target.href;
    if (allMatches[url]) {
      allMatches[url].click_count = (allMatches[url].click_count || 0) + 1;
      fetch('/api/click', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({url}),
      });
      render();
    }
  }
});

document.addEventListener('change', (e) => {
  if (e.target.matches('.notes-input')) {
    const url = decodeURIComponent(e.target.dataset.url);
    const notes = e.target.value;
    allMatches[url].notes = notes;
    fetch('/api/notes', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url, notes}),
    });
  }
});

document.addEventListener('keydown', (e) => {
  if (e.target.matches('.notes-input') && e.key === 'Enter') {
    e.target.blur();
  }
});

load();

// SSE: instant update when the server detects matches.json changed
const es = new EventSource('/api/events');
es.onmessage = (e) => { if (e.data === 'update') poll(); };
// fallback polling — catches anything SSE misses (e.g. reconnection gaps)
setInterval(poll, 60000);
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
        if self.path in ("/", "/index.html"):
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/matches":
            self._send_json(scraper.load_matches_store())
        elif self.path == "/api/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            with _sse_lock:
                _sse_clients.append(self.wfile)
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while True:
                    time.sleep(30)
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except OSError:
                pass
            finally:
                with _sse_lock:
                    if self.wfile in _sse_clients:
                        _sse_clients.remove(self.wfile)
        else:
            self.send_response(404)
            self.end_headers()

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length))

    def do_POST(self):
        if self.path == "/api/status":
            payload = self._read_json()
            url, status = payload.get("url"), payload.get("status")
            if status not in scraper.STATUSES:
                self._send_json({"error": "invalid status"}, status=400)
                return
            store = scraper.load_matches_store()
            if url not in store:
                self._send_json({"error": "unknown url"}, status=404)
                return
            old_status = store[url].get("status", "new")
            store[url]["status"] = status
            if status == "cv_sent":
                store[url]["cv_sent_at"] = datetime.now().isoformat(timespec="seconds")
            elif old_status == "cv_sent" and status == "new":
                store[url]["cv_sent_at"] = None
            scraper.save_matches_store(store)
            self._send_json({"ok": True, "cv_sent_at": store[url].get("cv_sent_at")})
        elif self.path == "/api/notes":
            payload = self._read_json()
            url, notes = payload.get("url"), payload.get("notes", "")
            store = scraper.load_matches_store()
            if url not in store:
                self._send_json({"error": "unknown url"}, status=404)
                return
            store[url]["notes"] = notes
            scraper.save_matches_store(store)
            self._send_json({"ok": True})
        elif self.path == "/api/click":
            payload = self._read_json()
            url = payload.get("url")
            store = scraper.load_matches_store()
            if url not in store:
                self._send_json({"error": "unknown url"}, status=404)
                return
            store[url]["click_count"] = store[url].get("click_count", 0) + 1
            scraper.save_matches_store(store)
            self._send_json({"ok": True, "click_count": store[url]["click_count"]})
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    watcher = threading.Thread(target=_watch_matches_file, daemon=True)
    watcher.start()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Serving job match browser at {url} (Ctrl+C to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
