"""MCP server exposing the job-scraper webui's data and actions as tools.

Wraps the same matches.json store the webui (webui.py) reads and writes,
via the shared mutation helpers in scraper.py, so an MCP client (Claude
Desktop, Claude Code, etc.) can browse and triage job matches directly.
"""

import argparse

from mcp.server.fastmcp import FastMCP

import scraper

mcp = FastMCP("job-scraper")


def _with_url(url, m):
    return {"url": url, **m}


@mcp.tool()
def list_matches(status: str | None = None, skill: str | None = None) -> list[dict]:
    """List job matches, highest score first.

    status: filter to one of scraper.STATUSES (new, interesting, cv_sent,
        expired, not_for_me). Omit for all statuses.
    skill: filter to matches whose skills list contains this exact skill
        name (see list_skills for available names). Omit for all skills.
    """
    store = scraper.load_matches_store()
    results = [
        _with_url(url, m)
        for url, m in store.items()
        if (status is None or m.get("status", "new") == status)
        and (skill is None or skill in (m.get("skills") or []))
    ]
    results.sort(key=lambda m: m.get("score", 0), reverse=True)
    return results


@mcp.tool()
def get_match(url: str) -> dict:
    """Get full details for a single job match by its offer URL."""
    store = scraper.load_matches_store()
    if url not in store:
        raise ValueError(f"unknown url: {url}")
    return _with_url(url, store[url])


@mcp.tool()
def set_match_status(url: str, status: str) -> dict:
    """Set a match's status: new, interesting, cv_sent, expired, or not_for_me.

    Setting cv_sent stamps cv_sent_at with the current time; reverting a
    cv_sent match back to new clears it.
    """
    try:
        m = scraper.update_match_status(url, status)
    except scraper.MatchNotFound:
        raise ValueError(f"unknown url: {url}")
    return _with_url(url, m)


@mcp.tool()
def set_match_notes(url: str, notes: str) -> dict:
    """Replace the free-text notes field on a match."""
    try:
        m = scraper.update_match_notes(url, notes)
    except scraper.MatchNotFound:
        raise ValueError(f"unknown url: {url}")
    return _with_url(url, m)


@mcp.tool()
def mark_match_opened(url: str) -> dict:
    """Record that the offer link was opened, incrementing click_count."""
    try:
        m = scraper.record_match_click(url)
    except scraper.MatchNotFound:
        raise ValueError(f"unknown url: {url}")
    return _with_url(url, m)


@mcp.tool()
def list_skills() -> list[dict]:
    """List every skill seen across all matches with its total occurrence count,
    highest first. Use this to discover valid `skill` values for list_matches
    and skill_occurrence_stats."""
    totals: dict[str, int] = {}
    for m in scraper.load_matches_store().values():
        for s in m.get("skills") or []:
            totals[s] = totals.get(s, 0) + 1
    return [
        {"skill": s, "total": n}
        for s, n in sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    ]


@mcp.tool()
def skill_occurrence_stats(skill: str | None = None) -> dict:
    """Daily occurrence counts per skill, keyed by skill then by first_seen
    date (YYYY-MM-DD). Mirrors the chart on the webui's /stats page.

    skill: limit to one skill's series. Omit for every skill's series.
    """
    counts: dict[str, dict[str, int]] = {}
    for m in scraper.load_matches_store().values():
        first_seen = m.get("first_seen")
        if not first_seen:
            continue
        date = first_seen[:10]
        for s in m.get("skills") or []:
            if skill is not None and s != skill:
                continue
            counts.setdefault(s, {})
            counts[s][date] = counts[s].get(date, 0) + 1
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="MCP transport (default: stdio, for use as a subprocess MCP server).",
    )
    parser.add_argument(
        "--host",
        default=mcp.settings.host,
        help=f"Host to bind for sse/streamable-http transports (default: {mcp.settings.host}).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=mcp.settings.port,
        help=f"Port to bind for sse/streamable-http transports (default: {mcp.settings.port}).",
    )
    args = parser.parse_args()
    mcp.settings.host = args.host
    mcp.settings.port = args.port
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
