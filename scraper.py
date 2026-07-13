import argparse
import json
import logging
import re
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path

import requests


from sources.log_config import log_config
from profile import PROFILE
from sources import bulldogjob, justjoin, nofluffjobs, rocketjobs, solidjobs, theprotocol

# Sources that all expose the same list_candidate_urls(prefilter_keywords,
# target_employers) -> [url] signature (sitemap + slug prefilter discovery).
logger = log_config(name=__name__, log_file=f"/var/log/{__name__}/{__name__}.log")

SITEMAP_SOURCES = [justjoin, theprotocol, rocketjobs, solidjobs, bulldogjob]

REQUEST_DELAY_SECONDS = 0.3

BASE_DIR = Path(__file__).resolve().parent
STATE_PATH = BASE_DIR / "data" / "seen_urls.json"
LOG_PATH = BASE_DIR / "matches.log"
MATCHES_STORE_PATH = BASE_DIR / "data" / "matches.json"
MD_PATH = BASE_DIR / "matches.md"

STATUSES = ("new", "interesting", "cv_sent", "not_for_me")
STATUS_LABELS = {
    "new": "New",
    "interesting": "Interesting",
    "cv_sent": "CV sent",
    "not_for_me": "Not for me",
}

LOG_LINE_RE = re.compile(
    r"^\[(?P<ts>[^\]]+)\] \(score (?P<score>-?\d+)\) \[(?P<source>[^\]]+)\] "
    r"(?P<title>.+?) @ (?P<company>.+?)(?: \[TARGET EMPLOYER\])? "
    r"\((?P<where>[^)]+)\) skills: (?P<skills>.+?) -> (?P<url>\S+)$"
)


def text_contains(keyword, text):
    if re.fullmatch(r"[a-z0-9]+", keyword):
        return re.search(rf"\b{re.escape(keyword)}\b", text) is not None
    return keyword in text


def find_weighted_hits(text, weights):
    return {kw: weight for kw, weight in weights.items() if text_contains(kw, text)}


def normalize_title(title):
    return re.sub(r"\s+", " ", title.strip().lower())


def discover_candidates():
    candidates = {}

    for module in SITEMAP_SOURCES:
        logger.info(f"Discovering candidates on {module.NAME}...", file=sys.stderr)
        try:
            urls = module.list_candidate_urls(
                PROFILE["slug_prefilter_keywords"], PROFILE["target_employers"]
            )
        except requests.RequestException as exc:
            logger.info(f"  failed to discover {module.NAME}: {exc}", file=sys.stderr)
            continue
        for url in urls:
            candidates.setdefault(url, (module, None))

    logger.info("Discovering candidates on nofluffjobs.com...", file=sys.stderr)
    try:
        nfj_hints = nofluffjobs.list_candidate_urls(PROFILE["nofluffjobs_categories"])
    except requests.RequestException as exc:
        logger.info(f"  failed to discover nofluffjobs: {exc}", file=sys.stderr)
        nfj_hints = {}
    for url, hint in nfj_hints.items():
        candidates.setdefault(url, (nofluffjobs, hint))

    return candidates


def evaluate_offer(normalized, url):
    title = normalized["title"]
    description = normalized["description"]
    text = f"{title}\n{description}".lower()

    positive_hits = find_weighted_hits(text, PROFILE["skill_weights"])
    negative_hits = find_weighted_hits(text, PROFILE["negative_weights"])
    score = sum(positive_hits.values()) + sum(negative_hits.values())

    company = normalized["company"]
    is_target_employer = company.lower() in PROFILE["target_employers"]
    if is_target_employer:
        score += PROFILE["target_employer_bonus"]

    localities = normalized["localities"]
    is_remote = PROFILE["location"]["remote_ok"] and normalized["is_remote"]
    is_target_city = any(loc.lower() in PROFILE["location"]["city_keywords"] for loc in localities)
    location_match = is_remote or is_target_city

    is_b2b = normalized["is_b2b"] or text_contains("b2b", text)
    if is_b2b:
        score += PROFILE["b2b_bonus"]

    is_match = location_match and score >= PROFILE["score_threshold"]

    return {
        "url": url,
        "source": normalized["source"],
        "title": title,
        "company": company,
        "localities": localities,
        "remote": is_remote,
        "b2b": is_b2b,
        "score": score,
        "positive_hits": positive_hits,
        "negative_hits": negative_hits,
        "is_target_employer": is_target_employer,
        "is_match": is_match,
    }


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def append_matches_log(matches):
    if not matches:
        return
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = []
    for m in sorted(matches, key=lambda m: m["score"], reverse=True):
        skills = ", ".join(m["positive_hits"].keys()) or "-"
        tag = " [TARGET EMPLOYER]" if m["is_target_employer"] else ""
        where = "remote" if m["remote"] else (", ".join(m["localities"]) or "unknown")
        lines.append(
            f"[{timestamp}] (score {m['score']}) [{m['source']}] {m['title']} @ {m['company']}{tag} "
            f"({where}) skills: {skills} -> {m['url']}"
        )
    with LOG_PATH.open("a") as f:
        f.write("\n".join(lines) + "\n")


def load_matches_store():
    if MATCHES_STORE_PATH.exists():
        return json.loads(MATCHES_STORE_PATH.read_text())
    # First run after upgrading: rebuild from the existing plain-text log
    # so previously found matches show up in the browser UI too.
    store = {}
    if LOG_PATH.exists():
        for line in LOG_PATH.read_text().splitlines():
            m = LOG_LINE_RE.match(line)
            if not m:
                continue
            skills = [s.strip() for s in m.group("skills").split(",") if s.strip() and s.strip() != "-"]
            where = m.group("where")
            store[m.group("url")] = {
                "title": m.group("title"),
                "company": m.group("company"),
                "source": m.group("source"),
                "where": where,
                "remote": where == "remote",
                "b2b": False,
                "score": int(m.group("score")),
                "skills": skills,
                "flags": [],
                "target_employer": "[TARGET EMPLOYER]" in line,
                "first_seen": m.group("ts"),
                "status": "new",
                "notes": "",
                "cv_sent_at": None,
                "click_count": 0,
            }
    return store


def save_matches_store(store):
    MATCHES_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    MATCHES_STORE_PATH.write_text(json.dumps(store, indent=2))


def dedup_new_matches(matches):
    """Collapse same-run matches that are the same job posted under multiple
    URLs (e.g. one ad per city), so the log/notification/store each only see
    it once per run instead of once per URL."""
    groups = {}
    order = []
    for m in matches:
        key = (m["company"].strip().lower(), normalize_title(m["title"]))
        if key not in groups:
            order.append(key)
        groups.setdefault(key, []).append(m)

    deduped = []
    for key in order:
        entries = groups[key]
        if len(entries) == 1:
            deduped.append(entries[0])
            continue
        best = max(entries, key=lambda m: m["score"])
        merged_localities = []
        for m in entries:
            for loc in m["localities"]:
                if loc not in merged_localities:
                    merged_localities.append(loc)
        merged = dict(best)
        merged["localities"] = merged_localities
        merged["remote"] = any(m["remote"] for m in entries)
        deduped.append(merged)
    return deduped


STATUS_PRIORITY = {"cv_sent": 3, "interesting": 2, "not_for_me": 1, "new": 0}


def dedup_store(store):
    groups = {}
    for url, m in store.items():
        key = (m["company"].strip().lower(), normalize_title(m["title"]))
        groups.setdefault(key, []).append((url, m))

    def location_rank(entry):
        where = entry[1].get("where", "")
        if where == "remote":
            return 0
        if where.lower() in PROFILE["location"]["city_keywords"]:
            return 1
        return 2

    deduped = {}
    for entries in groups.values():
        if len(entries) == 1:
            url, m = entries[0]
            deduped[url] = m
            continue
        # Prefer whichever duplicate has the most "advanced" human-applied
        # status, tie-broken by location, so notes/cv_sent_at/click_count
        # travel with the duplicate the user actually interacted with.
        best_status = max(
            (m.get("status", "new") for _, m in entries),
            key=lambda s: STATUS_PRIORITY.get(s, 0),
        )
        candidates = [e for e in entries if e[1].get("status", "new") == best_status]
        candidates.sort(key=location_rank)
        url, best = candidates[0]
        merged = dict(best)
        merged["click_count"] = sum(e[1].get("click_count", 0) for e in entries)
        other_locations = sorted({e[1]["where"] for e in entries if e[1]["where"] != best["where"]})
        if other_locations:
            merged["also_in"] = other_locations
        deduped[url] = merged
    return deduped


def update_matches_store(new_matches):
    store = load_matches_store()
    now = datetime.now().isoformat(timespec="seconds")
    for m in new_matches:
        store[m["url"]] = {
            "title": m["title"],
            "company": m["company"],
            "source": m["source"],
            "where": "remote" if m["remote"] else (", ".join(m["localities"]) or "unknown"),
            "remote": m["remote"],
            "b2b": m["b2b"],
            "score": m["score"],
            "skills": sorted(m["positive_hits"].keys()),
            "flags": sorted(m["negative_hits"].keys()),
            "target_employer": m["is_target_employer"],
            "first_seen": now,
            "status": "new",
            "notes": "",
            "cv_sent_at": None,
            "click_count": 0,
        }
    store = dedup_store(store)
    save_matches_store(store)


def export_markdown(store, path=MD_PATH):
    by_status = {s: [] for s in STATUSES}
    for url, m in store.items():
        by_status.setdefault(m.get("status", "new"), []).append((url, m))

    lines = [
        "# Job Matches Export",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "Sources: justjoin.it, nofluffjobs.com, theprotocol.it -- matched against the profile in profile.py",
        f"Total: {len(store)} offers",
        "",
    ]
    for status in STATUSES:
        entries = sorted(
            by_status.get(status, []),
            key=lambda kv: kv[1].get("score", 0),
            reverse=True,
        )
        lines.append(f"## {STATUS_LABELS[status]} ({len(entries)})")
        lines.append("")
        if not entries:
            lines.append("_None._")
            lines.append("")
            continue
        for url, m in entries:
            lines.append(f"### {m['title']} — {m['company']} (score: {m.get('score', 0)})")
            lines.append(f"- Source: {m.get('source', 'unknown')}")
            where = m["where"]
            if m.get("also_in"):
                where += f" (+{len(m['also_in'])} other location(s): {', '.join(m['also_in'])})"
            lines.append(f"- Location: {where}")
            lines.append(f"- B2B: {'Yes' if m.get('b2b') else 'Unclear/No'}")
            lines.append(f"- Skills matched: {', '.join(m.get('skills', [])) or '-'}")
            if m.get("flags"):
                lines.append(f"- Caution flags: {', '.join(m['flags'])}")
            lines.append(f"- Target employer: {'Yes' if m['target_employer'] else 'No'}")
            lines.append(f"- First seen: {m['first_seen']}")
            if m.get("cv_sent_at"):
                lines.append(f"- CV sent at: {m['cv_sent_at']}")
            if m.get("notes"):
                lines.append(f"- Notes: {m['notes']}")
            lines.append(f"- Link: {url}")
            lines.append("")
    path.write_text("\n".join(lines))


NTFY_TOKEN_FILE = Path("/etc/job-scraper/ntfy-token")


def load_ntfy_token(path=NTFY_TOKEN_FILE):
    try:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    except OSError:
        pass
    return None


def _ntfy_post(server, topic, title, body, priority="default", click=None, token=None):
    headers = {
        "Title": title,
        "Tags": "briefcase",
        "Priority": priority,
    }
    if click:
        headers["Click"] = click
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.post(
        f"{server.rstrip('/')}/{topic}",
        data=body.encode(),
        headers=headers,
        timeout=10,
    )
    resp.raise_for_status()


def send_ntfy_test(topic, server="https://ntfy.sh", token=None):
    try:
        _ntfy_post(server, topic, "job-scraper: test notification", "ntfy is working correctly.", token=token)
        logger.info(f"Test notification sent to {server}/{topic}")
    except requests.RequestException as exc:
        logger.info(f"ntfy test failed: {exc}", file=sys.stderr)
        sys.exit(1)


def send_ntfy_notification(topic, new_matches, server="https://ntfy.sh", always=False, token=None):
    if not new_matches:
        if always:
            try:
                _ntfy_post(server, topic, "job-scraper: no new matches", "Scrape complete — nothing new this run.", priority="min", token=token)
                logger.info(f"ntfy notification sent (no new matches) to topic '{topic}'", file=sys.stderr)
            except requests.RequestException as exc:
                logger.info(f"ntfy notification failed: {exc}", file=sys.stderr)
        return

    top = new_matches[0]
    count = len(new_matches)
    if count == 1:
        title = f"New job match: {top['title']} @ {top['company']}"
        body = f"Score: {top['score']} | {top['source']}\n{top['url']}"
        click = top["url"]
    else:
        title = f"{count} new job matches found"
        lines = [
            f"• {m['title']} @ {m['company']} (score {m['score']})"
            for m in new_matches[:5]
        ]
        if count > 5:
            lines.append(f"… and {count - 5} more")
        body = "\n".join(lines)
        click = None

    priority = "high" if top["score"] >= 10 else "default"
    try:
        _ntfy_post(server, topic, title, body, priority=priority, click=click, token=token)
        logger.info(f"ntfy notification sent to topic '{topic}'", file=sys.stderr)
    except requests.RequestException as exc:
        logger.info(f"ntfy notification failed: {exc}", file=sys.stderr)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--open-browser",
        action="store_true",
        help="Open every newly matched offer in the default web browser.",
    )
    parser.add_argument(
        "--export-md",
        action="store_true",
        help="Write all matches, grouped by status, to matches.md for easy LLM analysis.",
    )
    parser.add_argument(
        "--ntfy-topic",
        metavar="TOPIC",
        help="Send a ntfy.sh push notification to this topic when new matches are found.",
    )
    parser.add_argument(
        "--ntfy-server",
        metavar="URL",
        default="https://ntfy.sh",
        help="Base URL of the ntfy server (default: https://ntfy.sh).",
    )
    parser.add_argument(
        "--ntfy-test",
        action="store_true",
        help="Send a test notification to verify ntfy is reachable, then exit.",
    )
    parser.add_argument(
        "--ntfy-always",
        action="store_true",
        help="Send an ntfy notification even when no new matches were found (priority: min).",
    )
    parser.add_argument(
        "--ntfy-token-file",
        metavar="FILE",
        default=str(NTFY_TOKEN_FILE),
        help=f"File containing the ntfy Bearer token (default: {NTFY_TOKEN_FILE}). "
             "Lines starting with # are ignored. Omit or leave empty for unauthenticated access.",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    ntfy_token = load_ntfy_token(args.ntfy_token_file) if args.ntfy_topic else None

    if args.ntfy_test:
        if not args.ntfy_topic:
            logger.info("--ntfy-test requires --ntfy-topic", file=sys.stderr)
            sys.exit(1)
        if ntfy_token:
            logger.info(f"Using token from {args.ntfy_token_file}", file=sys.stderr)
        else:
            logger.info("No token found — sending unauthenticated.", file=sys.stderr)
        send_ntfy_test(args.ntfy_topic, args.ntfy_server, token=ntfy_token)
        return

    state = load_state()

    candidates = discover_candidates()
    to_process = [u for u in candidates if u not in state]
    logger.info(
        f"{len(candidates)} total candidates across all sources, "
        f"{len(to_process)} new to check.",
        file=sys.stderr,
    )

    total = len(to_process)
    new_matches = []
    for i, url in enumerate(to_process, 1):
        logger.info(
            f"\r[{i}/{total}] checking offers... ({len(new_matches)} matches so far)",
            end="",
            file=sys.stderr,
            flush=True,
        )
        module, hint = candidates[url]
        try:
            normalized = module.fetch_offer(url, hint)
            if normalized is None:
                state[url] = {
                    "checked_at": datetime.now().isoformat(timespec="seconds"),
                    "matched": False,
                    "no_data": True,
                }
                continue
            result = evaluate_offer(normalized, url)
            state[url] = {
                "checked_at": datetime.now().isoformat(timespec="seconds"),
                "matched": result["is_match"],
            }
            if result["is_match"]:
                new_matches.append(result)
                logger.info(
                    f"\n  match (score {result['score']}, {result['source']}): "
                    f"{result['title']} @ {result['company']}",
                    file=sys.stderr,
                )
        except requests.RequestException as exc:
            logger.info(f"\nFailed to fetch {url}: {exc}", file=sys.stderr)
        time.sleep(REQUEST_DELAY_SECONDS)
    if total:
        logger.info(file=sys.stderr)

    new_matches = dedup_new_matches(new_matches)

    append_matches_log(new_matches)
    update_matches_store(new_matches)
    save_state(state)

    new_matches.sort(key=lambda m: m["score"], reverse=True)
    logger.info(f"Found {len(new_matches)} new matching offer(s).", file=sys.stderr)
    for m in new_matches:
        logger.info(f"  - (score {m['score']}, {m['source']}) {m['title']} @ {m['company']} -> {m['url']}")

    if args.ntfy_topic:
        send_ntfy_notification(args.ntfy_topic, new_matches, args.ntfy_server, always=args.ntfy_always, token=ntfy_token)

    if args.open_browser:
        for m in new_matches:
            webbrowser.open(m["url"])

    if args.export_md:
        export_markdown(load_matches_store())
        logger.info(f"Wrote {MD_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
