import argparse
import json
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import scraper

# Errors that just mean the client hung up mid-response (closed tab,
# navigated away) -- expected under normal use, not worth a traceback.
DISCONNECT_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)

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
  .actions button[data-status="expired"].active { background: #6a6a6a; }
  .actions button[data-status="not_for_me"].active { background: #b03030; }
  .row.status-not_for_me, .row.status-expired { opacity: 0.45; }
  .nav { margin-bottom: 1rem; }
  .nav a { color: #7fb0ff; margin-right: 1.2rem; text-decoration: none; font-size: 0.85rem; }
  .nav a:hover { text-decoration: underline; }
  .nav a.active { color: #fff; font-weight: bold; text-decoration: none; }
  .filters-row { display: flex; align-items: center; gap: 0.6rem; margin-bottom: 1rem; flex-wrap: wrap; }
  select.skill-select { background: #1a1a1a; color: #ddd; border: 1px solid #333;
                         border-radius: 6px; padding: 0.35rem 0.5rem; font-size: 0.85rem; }
</style>
</head>
<body>
<div class="nav"><a href="/" class="active">Matches</a><a href="/stats">Skill stats</a></div>
<h1>Job matches</h1>
<div class="filters-row">
  <div class="tabs" id="tabs"></div>
  <select id="skillSelect" class="skill-select"></select>
</div>
<table>
  <thead>
    <tr><th>Score</th><th>Offer</th><th>Source</th><th>Company</th><th>Where</th><th>Skills</th><th>Stored</th><th>Notes</th><th>Status</th></tr>
  </thead>
  <tbody id="rows"></tbody>
</table>
<script>
let allMatches = {};
let currentFilter = 'all';
let currentSkillFilter = 'all';
const FILTERS = ['all', 'new', 'interesting', 'cv_sent', 'expired', 'not_for_me'];
const LABELS = {all: 'All', new: 'New', interesting: 'Interesting', cv_sent: 'CV sent', expired: 'Expired', not_for_me: 'Not for me'};

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

  const skillCounts = {};
  for (const m of Object.values(allMatches)) {
    for (const s of (m.skills || [])) skillCounts[s] = (skillCounts[s] || 0) + 1;
  }
  const skillSelect = document.getElementById('skillSelect');
  const skills = Object.keys(skillCounts).sort((a, b) => skillCounts[b] - skillCounts[a]);
  const skillOptions = ['all', ...skills];
  if (!skillOptions.includes(currentSkillFilter)) currentSkillFilter = 'all';
  skillSelect.innerHTML = skillOptions.map(s =>
    `<option value="${escapeHtml(s)}" ${s === currentSkillFilter ? 'selected' : ''}>${s === 'all' ? 'All skills' : `${escapeHtml(s)} (${skillCounts[s]})`}</option>`
  ).join('');

  const entries = Object.entries(allMatches)
    .filter(([url, m]) => currentFilter === 'all' || m.status === currentFilter)
    .filter(([url, m]) => currentSkillFilter === 'all' || (m.skills || []).includes(currentSkillFilter))
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
        ${['interesting', 'cv_sent', 'expired', 'not_for_me'].map(s =>
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
  if (e.target.matches('#skillSelect')) {
    currentSkillFilter = e.target.value;
    render();
  } else if (e.target.matches('.notes-input')) {
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

STATS_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Skill stats</title>
<style>
  :root {
    --surface: #1a1a19;
    --page: #111;
    --ink-primary: #ffffff;
    --ink-secondary: #c3c2b7;
    --ink-muted: #898781;
    --gridline: #2c2c2a;
    --axis: #383835;
    --series-1: #3987e5;
    --series-2: #199e70;
    --series-3: #c98500;
    --series-4: #008300;
    --series-5: #9085e9;
    --series-6: #e66767;
    --series-7: #d55181;
    --series-8: #d95926;
  }
  body { font-family: system-ui, sans-serif; margin: 2rem; background: var(--page); color: #ddd; }
  h1 { font-size: 1.3rem; }
  .nav { margin-bottom: 1rem; }
  .nav a { color: #7fb0ff; margin-right: 1.2rem; text-decoration: none; font-size: 0.85rem; }
  .nav a:hover { text-decoration: underline; }
  .nav a.active { color: #fff; font-weight: bold; text-decoration: none; }
  .card { background: var(--surface); border: 1px solid rgba(255,255,255,0.10); border-radius: 8px;
          padding: 1rem 1.2rem; margin-bottom: 1rem; }
  .empty { color: var(--ink-muted); font-size: 0.9rem; }
  .legend { display: flex; flex-wrap: wrap; gap: 0.9rem; margin-bottom: 0.8rem; }
  .legend-item { display: flex; align-items: center; gap: 0.4rem; font-size: 0.85rem;
                 color: var(--ink-secondary); cursor: pointer; user-select: none; }
  .legend-item input { accent-color: #3987e5; }
  .legend-item .swatch { width: 14px; height: 3px; border-radius: 2px; display: inline-block; }
  .legend-item .count { color: var(--ink-muted); }
  .chart-wrap { position: relative; }
  svg#chart { width: 100%; height: auto; display: block; overflow: visible; }
  .gridline { stroke: var(--gridline); stroke-width: 1; }
  .axis-line { stroke: var(--axis); stroke-width: 1; }
  .tick-label { fill: var(--ink-muted); font-size: 11px; }
  .direct-label { fill: var(--ink-secondary); font-size: 11px; }
  .crosshair { stroke: var(--axis); stroke-width: 1; pointer-events: none; }
  .tooltip { position: absolute; background: #222; border: 1px solid #444; border-radius: 6px;
             padding: 0.5rem 0.7rem; font-size: 0.8rem; pointer-events: none; white-space: nowrap;
             transform: translate(-50%, -110%); z-index: 5; }
  .tooltip .ttl { color: var(--ink-muted); margin-bottom: 0.3rem; }
  .tooltip .row { display: flex; align-items: center; gap: 0.4rem; }
  .tooltip .row .key { width: 10px; height: 2px; display: inline-block; }
  .tooltip .row .val { color: var(--ink-primary); font-weight: bold; margin-left: auto; padding-left: 0.6rem; }
  .tooltip .row .name { color: var(--ink-secondary); }
  .toggle-table { background: #222; color: #ccc; border: 1px solid #444; padding: 0.35rem 0.7rem;
                  border-radius: 6px; cursor: pointer; font-size: 0.8rem; margin-bottom: 0.8rem; }
  table { width: 100%; border-collapse: collapse; }
  td, th { padding: 0.4rem 0.6rem; border-bottom: 1px solid #333; text-align: left; font-size: 0.85rem; }
  th { color: #999; font-size: 0.75rem; text-transform: uppercase; }
  #tableView { display: none; }
</style>
</head>
<body>
<div class="nav"><a href="/">Matches</a><a href="/stats" class="active">Skill stats</a></div>
<h1>Skill occurrences over time</h1>
<div class="card" id="app">
  <div class="empty" id="emptyMsg" style="display:none;">Not enough data yet — run the scraper a few times first.</div>
  <div id="chartArea">
    <div class="legend" id="legend"></div>
    <button class="toggle-table" id="toggleTable">Show as table</button>
    <div class="chart-wrap" id="chartWrap">
      <svg id="chart" viewBox="0 0 760 320" preserveAspectRatio="none"></svg>
      <div class="tooltip" id="tooltip" hidden></div>
    </div>
    <div id="tableView"></div>
  </div>
</div>
<script>
const SERIES_COLORS = ['var(--series-1)', 'var(--series-2)', 'var(--series-3)', 'var(--series-4)',
                        'var(--series-5)', 'var(--series-6)', 'var(--series-7)', 'var(--series-8)'];
const MAX_SKILLS = 8;
const DEFAULT_VISIBLE = 5;
const MARGIN = {top: 16, right: 20, bottom: 32, left: 36};
const W = 760, H = 320;

let topSkills = [];       // [{skill, color, total}], fixed rank order
let visibleSkills = new Set();
let allDates = [];        // sorted ascending 'YYYY-MM-DD'
let skillDateCounts = {}; // skill -> {date: count}
let showTable = false;

function escapeHtml(s) {
  return (s || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function formatDateShort(d) {
  const dt = new Date(d + 'T00:00:00');
  return dt.toLocaleDateString(undefined, {month: 'short', day: 'numeric'});
}

function niceNum(range, round) {
  const exponent = Math.floor(Math.log10(range || 1));
  const fraction = (range || 1) / Math.pow(10, exponent);
  let niceFraction;
  if (round) {
    niceFraction = fraction < 1.5 ? 1 : fraction < 3 ? 2 : fraction < 7 ? 5 : 10;
  } else {
    niceFraction = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10;
  }
  return niceFraction * Math.pow(10, exponent);
}

function niceTicks(max, maxTicks) {
  const step = Math.max(1, Math.round(niceNum(Math.max(max, 1) / (maxTicks - 1), true)));
  const niceMax = Math.ceil(Math.max(max, 1) / step) * step;
  const ticks = [];
  for (let v = 0; v <= niceMax; v += step) ticks.push(v);
  return ticks;
}

async function load() {
  const res = await fetch('/api/matches');
  const matches = await res.json();

  skillDateCounts = {};
  const totals = {};
  const dateSet = new Set();
  for (const m of Object.values(matches)) {
    if (!m.first_seen) continue;
    const date = m.first_seen.slice(0, 10);
    dateSet.add(date);
    for (const skill of (m.skills || [])) {
      skillDateCounts[skill] = skillDateCounts[skill] || {};
      skillDateCounts[skill][date] = (skillDateCounts[skill][date] || 0) + 1;
      totals[skill] = (totals[skill] || 0) + 1;
    }
  }
  allDates = [...dateSet].sort();

  const rankedSkills = Object.keys(totals).sort((a, b) => totals[b] - totals[a]).slice(0, MAX_SKILLS);
  topSkills = rankedSkills.map((skill, i) => ({skill, color: SERIES_COLORS[i], total: totals[skill]}));
  visibleSkills = new Set(topSkills.slice(0, DEFAULT_VISIBLE).map(s => s.skill));

  render();
}

function render() {
  document.getElementById('emptyMsg').style.display = (allDates.length === 0 || topSkills.length === 0) ? 'block' : 'none';
  document.getElementById('chartArea').style.display = (allDates.length === 0 || topSkills.length === 0) ? 'none' : 'block';
  if (allDates.length === 0 || topSkills.length === 0) return;

  renderLegend();
  renderChart();
  renderTable();
}

function renderLegend() {
  document.getElementById('legend').innerHTML = topSkills.map(s => `
    <label class="legend-item">
      <input type="checkbox" data-skill="${escapeHtml(s.skill)}" ${visibleSkills.has(s.skill) ? 'checked' : ''}>
      <span class="swatch" style="background:${s.color}"></span>
      ${escapeHtml(s.skill)} <span class="count">(${s.total})</span>
    </label>
  `).join('');
}

function xScale(dateIdx) {
  const plotW = W - MARGIN.left - MARGIN.right;
  if (allDates.length <= 1) return MARGIN.left + plotW / 2;
  return MARGIN.left + (dateIdx / (allDates.length - 1)) * plotW;
}

function renderChart() {
  const svg = document.getElementById('chart');
  const visible = topSkills.filter(s => visibleSkills.has(s.skill));
  const plotH = H - MARGIN.top - MARGIN.bottom;

  let maxCount = 0;
  for (const s of visible) {
    for (const d of allDates) maxCount = Math.max(maxCount, (skillDateCounts[s.skill][d] || 0));
  }
  const ticks = niceTicks(maxCount, 5);
  const yMax = ticks[ticks.length - 1] || 1;
  const yScale = v => MARGIN.top + plotH - (v / yMax) * plotH;

  let svgParts = [];

  // gridlines + y ticks
  for (const t of ticks) {
    const y = yScale(t);
    svgParts.push(`<line class="gridline" x1="${MARGIN.left}" x2="${W - MARGIN.right}" y1="${y}" y2="${y}"/>`);
    svgParts.push(`<text class="tick-label" x="${MARGIN.left - 8}" y="${y + 3}" text-anchor="end">${t}</text>`);
  }
  // x axis baseline + date ticks
  const axisY = MARGIN.top + plotH;
  svgParts.push(`<line class="axis-line" x1="${MARGIN.left}" x2="${W - MARGIN.right}" y1="${axisY}" y2="${axisY}"/>`);
  const numXTicks = Math.min(allDates.length, 6);
  for (let i = 0; i < numXTicks; i++) {
    const idx = numXTicks === 1 ? 0 : Math.round(i * (allDates.length - 1) / (numXTicks - 1));
    const x = xScale(idx);
    svgParts.push(`<text class="tick-label" x="${x}" y="${axisY + 18}" text-anchor="middle">${formatDateShort(allDates[idx])}</text>`);
  }

  // series lines
  for (const s of visible) {
    const pts = allDates.map((d, i) => [xScale(i), yScale(skillDateCounts[s.skill][d] || 0)]);
    if (pts.length === 1) {
      svgParts.push(`<circle cx="${pts[0][0]}" cy="${pts[0][1]}" r="5" fill="${s.color}" stroke="var(--surface)" stroke-width="2"/>`);
    } else {
      const d = pts.map((p, i) => (i === 0 ? 'M' : 'L') + p[0] + ',' + p[1]).join(' ');
      svgParts.push(`<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>`);
    }
    const last = pts[pts.length - 1];
    svgParts.push(`<circle cx="${last[0]}" cy="${last[1]}" r="5" fill="${s.color}" stroke="var(--surface)" stroke-width="2"/>`);
    if (visible.length <= 4) {
      svgParts.push(`<text class="direct-label" x="${last[0] + 8}" y="${last[1] + 4}">${escapeHtml(s.skill)}</text>`);
    }
  }

  // crosshair (hidden by default) + hit layer
  svgParts.push(`<line id="crosshair" class="crosshair" x1="0" x2="0" y1="${MARGIN.top}" y2="${axisY}" style="display:none"/>`);
  svgParts.push(`<rect id="hitLayer" x="${MARGIN.left}" y="${MARGIN.top}" width="${W - MARGIN.left - MARGIN.right}" height="${plotH}" fill="transparent"/>`);

  svg.innerHTML = svgParts.join('');
  wireHover(visible);
}

function wireHover(visible) {
  const svg = document.getElementById('chart');
  const hitLayer = document.getElementById('hitLayer');
  const crosshair = document.getElementById('crosshair');
  const tooltip = document.getElementById('tooltip');
  const wrap = document.getElementById('chartWrap');

  function nearestIdx(clientX) {
    const rect = svg.getBoundingClientRect();
    const svgX = (clientX - rect.left) / rect.width * W;
    let best = 0, bestDist = Infinity;
    for (let i = 0; i < allDates.length; i++) {
      const dist = Math.abs(xScale(i) - svgX);
      if (dist < bestDist) { bestDist = dist; best = i; }
    }
    return best;
  }

  function showAt(clientX, clientY) {
    const idx = nearestIdx(clientX);
    const x = xScale(idx);
    crosshair.setAttribute('x1', x);
    crosshair.setAttribute('x2', x);
    crosshair.style.display = 'block';

    const date = allDates[idx];
    const rows = visible.map(s => `
      <div class="row">
        <span class="key" style="background:${s.color}"></span>
        <span class="name">${escapeHtml(s.skill)}</span>
        <span class="val">${skillDateCounts[s.skill][date] || 0}</span>
      </div>
    `).join('');
    tooltip.innerHTML = `<div class="ttl">${escapeHtml(formatDateShort(date))}</div>${rows}`;
    tooltip.hidden = false;
    const wrapRect = wrap.getBoundingClientRect();
    tooltip.style.left = (clientX - wrapRect.left) + 'px';
    tooltip.style.top = (clientY - wrapRect.top) + 'px';
  }

  hitLayer.addEventListener('pointermove', (e) => showAt(e.clientX, e.clientY));
  hitLayer.addEventListener('pointerleave', () => {
    crosshair.style.display = 'none';
    tooltip.hidden = true;
  });
}

function renderTable() {
  const visible = topSkills.filter(s => visibleSkills.has(s.skill));
  const rows = [];
  for (const d of [...allDates].reverse()) {
    for (const s of visible) {
      const count = skillDateCounts[s.skill][d] || 0;
      if (count > 0) rows.push({date: d, skill: s.skill, count});
    }
  }
  const el = document.getElementById('tableView');
  el.style.display = showTable ? 'block' : 'none';
  el.innerHTML = `<table><thead><tr><th>Date</th><th>Skill</th><th>Occurrences</th></tr></thead><tbody>${
    rows.map(r => `<tr><td>${escapeHtml(r.date)}</td><td>${escapeHtml(r.skill)}</td><td>${r.count}</td></tr>`).join('')
  }</tbody></table>`;
}

document.addEventListener('change', (e) => {
  if (e.target.matches('.legend-item input')) {
    const skill = e.target.dataset.skill;
    if (e.target.checked) visibleSkills.add(skill); else visibleSkills.delete(skill);
    render();
  }
});

document.getElementById('toggleTable').addEventListener('click', () => {
  showTable = !showTable;
  document.getElementById('toggleTable').textContent = showTable ? 'Hide table' : 'Show as table';
  renderTable();
});

load();
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
        elif self.path in ("/stats", "/stats.html"):
            body = STATS_PAGE.encode()
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
            try:
                m = scraper.update_match_status(url, status)
            except scraper.MatchNotFound:
                self._send_json({"error": "unknown url"}, status=404)
                return
            self._send_json({"ok": True, "cv_sent_at": m.get("cv_sent_at")})
        elif self.path == "/api/notes":
            payload = self._read_json()
            url, notes = payload.get("url"), payload.get("notes", "")
            try:
                scraper.update_match_notes(url, notes)
            except scraper.MatchNotFound:
                self._send_json({"error": "unknown url"}, status=404)
                return
            self._send_json({"ok": True})
        elif self.path == "/api/click":
            payload = self._read_json()
            url = payload.get("url")
            try:
                m = scraper.record_match_click(url)
            except scraper.MatchNotFound:
                self._send_json({"error": "unknown url"}, status=404)
                return
            self._send_json({"ok": True, "click_count": m["click_count"]})
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


class Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        exc_type = sys.exc_info()[0]
        if exc_type is not None and issubclass(exc_type, DISCONNECT_ERRORS):
            return
        super().handle_error(request, client_address)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    watcher = threading.Thread(target=_watch_matches_file, daemon=True)
    watcher.start()

    server = Server(("127.0.0.1", args.port), Handler)
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
