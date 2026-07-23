# job-scraper

Personal job offer scraper and matcher. Discovers offers across multiple Polish job boards, scores them against a weighted skill profile, and sends push notifications via [ntfy](https://ntfy.sh) for new matches.

## How it works

1. **Discovery** — each source walks its sitemap and filters URLs by slug keywords, producing a candidate list without fetching full pages.
2. **Evaluation** — each unseen candidate is fetched, normalized, and scored. Score = sum of matched skill weights + bonuses for B2B and target employers − penalties for negative keywords. Offers below `score_threshold` or outside the target location are skipped.
3. **Persistence** — seen URLs are stored in `data/seen_urls.json` so re-runs only process new offers. Matches are stored in `data/matches.json` and appended to `matches.log`.
4. **Notification** — if `--ntfy-topic` is set, a push notification is sent with the match summary.

## Sources

| Source | Site |
|---|---|
| justjoin | justjoin.it |
| theprotocol | theprotocol.it |
| rocketjobs | rocketjobs.pl |
| solidjobs | solid.jobs |
| bulldogjob | bulldogjob.pl |
| nofluffjobs | nofluffjobs.com |

## Requirements

- Python 3.10+
- `requests` (scraper)
- `mcp` (only needed to run `mcp_server.py`)
- `uv` (optional, used by `install.sh` when available)

## Quick start

```sh
python3 -m venv venv && source venv/bin/activate
pip install requests
python scraper.py
```

## CLI

```
python scraper.py [OPTIONS]

  --ntfy-topic TOPIC     Send a push notification to this ntfy topic on new matches
  --ntfy-server URL      ntfy server base URL (default: https://ntfy.sh)
  --open-browser         Open every new match in the default browser
  --export-md            Write all matches grouped by status to matches.md
```

## Web UI

A local browser UI for reviewing and triaging matches:

```sh
python webui.py          # opens http://localhost:8080
python webui.py --port 9000
```

Features: tab per status (New / Interesting / CV sent / Expired / Not for me), inline notes, click tracking, one-click status transitions.

## MCP server

`mcp_server.py` exposes the same `data/matches.json` store as the web UI, as MCP tools, so an MCP client (Claude Desktop, Claude Code, etc.) can browse and triage matches directly.

```sh
python mcp_server.py                 # stdio transport, for use as a subprocess MCP server
python mcp_server.py --transport sse --port 8080
```

Tools: `list_matches`, `get_match`, `set_match_status`, `set_match_notes`, `mark_match_opened`, `list_skills`, `skill_occurrence_stats`.

If installed via `install.sh`, two ways to reach it are set up automatically:

- **On demand (stdio)** — `job-scraper-mcp`, a wrapper that runs the installed venv against the same `/opt/job-scraper/data/matches.json` the cron scraper and web UI use. Point a local MCP client (Claude Desktop, Claude Code) at this command and it spawns/stops the server per session.
- **Always on (streamable-http)** — the `job-scraper-mcp` OpenRC service, listening on `http://127.0.0.1:8766` by default (`MCP_HOST` / `MCP_PORT` in `install.sh`). Useful for MCP clients that connect over HTTP instead of spawning a subprocess.

```sh
/usr/local/bin/job-scraper-mcp                # on-demand, stdio
rc-service job-scraper-mcp status              # check the always-on service
```

**Connecting from another machine**: `MCP_HOST` defaults to `127.0.0.1`, so the service is unreachable from anywhere but the box itself — that's the expected cause if a remote client can't connect. Either:

- SSH-tunnel instead of exposing the port: `ssh -L 8766:127.0.0.1:8766 user@host`, then point the client at `http://127.0.0.1:8766` locally, or
- Set `MCP_HOST="0.0.0.0"` (or a specific LAN IP) in `install.sh` and reinstall. `mcp_server.py` has **no authentication** — anyone who can reach the port can read and change your match data — so pair this with a firewall rule restricting the source IPs allowed to hit `MCP_PORT`. `install.sh` prints a warning at install time whenever `MCP_HOST` isn't loopback, as a reminder.

Example Claude Desktop / Claude Code config entry (stdio):

```json
{
  "mcpServers": {
    "job-scraper": {
      "command": "/usr/local/bin/job-scraper-mcp"
    }
  }
}
```

## Profile

Edit `profile.py` to customise matching:

| Key | Purpose |
|---|---|
| `skill_weights` | Keyword → score bonus (positive moves offer up) |
| `negative_weights` | Keyword → score penalty (large negative drops offer below threshold) |
| `score_threshold` | Minimum score to count as a match |
| `target_employers` | Company names that get an extra bonus |
| `target_employer_bonus` | Score added for target employer hits |
| `b2b_bonus` | Score added when B2B contract is detected |
| `location.city_keywords` | Accepted city names |
| `location.remote_ok` | Accept remote offers |
| `slug_prefilter_keywords` | Broad keywords used to shrink the sitemap candidate set |
| `nofluffjobs_categories` | Topic categories used for nofluffjobs discovery |

## Installation as a system cron job

`install.sh` sets up the scraper to run every 15 minutes as a dedicated system user, with log rotation and optional ntfy notifications.

```sh
# Review and edit configuration at the top of the script first
sudo ./install.sh --help
sudo ./install.sh
```

What it sets up:

```
/opt/job-scraper/            jobscraper:jobscraper  750  (code + venv + data)
/var/log/job-scraper/        jobscraper:jobscraper  750  (cron.log)
/etc/job-scraper/ntfy-token  jobscraper:jobscraper  600  (fill in manually)
/usr/local/bin/job-scraper                               (wrapper)
```

Cron schedule: `*/15 * * * *`, wrapped in `flock` to prevent overlapping runs.

To remove:

```sh
sudo ./install.sh --uninstall
```

### ntfy notifications

Set `NTFY_TOPIC` in `install.sh` before installing, or pass it at runtime:

```sh
python scraper.py --ntfy-topic job-alerts
python scraper.py --ntfy-topic job-alerts --ntfy-server http://127.0.0.1:2586
```

- Single match: notification title with the offer title and company; tap opens the URL.
- Multiple matches: summary with up to 5 bullet points.
- Priority is set to `high` when the top match scores ≥ 10.

## Data files

| File | Content |
|---|---|
| `data/seen_urls.json` | All checked URLs with match result and timestamp |
| `data/matches.json` | Full match records with status, notes, score, skills |
| `matches.log` | Append-only human-readable log of matched offers |
| `matches.md` | Exported markdown (generated with `--export-md`) |
