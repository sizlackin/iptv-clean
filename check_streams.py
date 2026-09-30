#!/usr/bin/env python3
"""Check which stream links in playlist.m3u actually answer, and remember it.

Writes stream-status.json. Nothing is deleted from playlist.m3u; the addon
builder reads the status file and simply leaves out links that have been dead
for a while. A channel whose links are ALL dead is hidden from Stremio until
one of them comes back (a single good check brings it back straight away).

Deliberately cautious, so working channels aren't hidden by mistake:
- "Blocked" answers (401/403/451/429) usually mean the server doesn't like the
  checking computer's location, not that the stream is dead. They never count.
- A failed link is re-tried a minute later in the same run before it counts.
- A link only counts as dead after failing on DEAD_AFTER_DAYS different days
  in a row. At most one failure is counted per day, however often this runs.
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PLAYLIST = Path("playlist.m3u")
STATUS_FILE = Path("stream-status.json")

DEAD_AFTER_DAYS = 2
TIMEOUT = 15
WORKERS = 32
RETRY_DELAY = 60
READ_BYTES = 65536
DEFAULT_USER_AGENT = "VLC/3.0.20 LibVLC/3.0.20"

BLOCKED_CODES = {401, 403, 407, 429, 451}


def parse_playlist() -> dict[str, dict[str, str | None]]:
    """url -> {referrer, user_agent} for every link in the playlist."""
    links: dict[str, dict[str, str | None]] = {}
    referrer = user_agent = None
    for raw in PLAYLIST.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        lower = line.lower()
        if line.startswith("#EXTINF:"):
            referrer = user_agent = None
        elif lower.startswith("#extvlcopt:http-referrer="):
            referrer = line.split("=", 1)[1].strip()
        elif lower.startswith("#extvlcopt:http-user-agent="):
            user_agent = line.split("=", 1)[1].strip()
        elif line and not line.startswith("#"):
            links.setdefault(line, {"referrer": referrer, "user_agent": user_agent})
    return links


def fetch(url: str, headers: dict[str, str]) -> tuple[int, str, bytes]:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        body = response.read(READ_BYTES)
        return response.status, (response.headers.get("Content-Type") or "").lower(), body


def looks_like_playlist(body: bytes) -> bool:
    return b"#EXTM3U" in body[:2048].lstrip(b"\xef\xbb\xbf \r\n\t")


def first_variant(base_url: str, body: bytes) -> str | None:
    """For a master playlist, the first quality's URL (to check it too)."""
    lines = body.decode("utf-8", errors="replace").splitlines()
    for index, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            for candidate in lines[index + 1:]:
                candidate = candidate.strip()
                if candidate and not candidate.startswith("#"):
                    return urllib.parse.urljoin(base_url, candidate)
    return None


def probe(url: str, referrer: str | None, user_agent: str | None) -> tuple[str, str]:
    """Return (verdict, reason). verdict is ok, dead or blocked."""
    headers = {"User-Agent": user_agent or DEFAULT_USER_AGENT, "Accept": "*/*"}
    if referrer:
        headers["Referer"] = referrer
    try:
        status, content_type, body = fetch(url, headers)
        if looks_like_playlist(body):
            variant = first_variant(url, body)
            if variant:
                v_status, _, v_body = fetch(variant, headers)
                if not looks_like_playlist(v_body) and v_status == 200 and not v_body:
                    return "dead", "quality list is empty"
            return "ok", f"HTTP {status}"
        if "text/html" in content_type:
            return "dead", "web page instead of a stream"
        if body:
            return "ok", f"HTTP {status} {content_type or 'data'}"
        return "dead", "empty answer"
    except urllib.error.HTTPError as exc:
        if exc.code in BLOCKED_CODES:
            return "blocked", f"HTTP {exc.code}"
        return "dead", f"HTTP {exc.code}"
    except (socket.timeout, TimeoutError):
        return "dead", "no answer (timed out)"
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return "dead", "no answer (timed out)"
        if isinstance(reason, ssl.SSLError):
            return "dead", "security certificate problem"
        return "dead", f"can't connect ({str(reason)[:80]})"
    except (ConnectionError, ssl.SSLError, OSError, ValueError) as exc:
        return "dead", f"error ({type(exc).__name__})"


def check_all(links: dict[str, dict[str, str | None]], urls: list[str]) -> dict[str, tuple[str, str]]:
    results: dict[str, tuple[str, str]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {
            pool.submit(probe, url, links[url]["referrer"], links[url]["user_agent"]): url
            for url in urls
        }
        for future in concurrent.futures.as_completed(futures):
            results[futures[future]] = future.result()
    return results


def main() -> None:
    links = parse_playlist()
    try:
        status = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        if not isinstance(status, dict):
            status = {}
    except (OSError, json.JSONDecodeError):
        status = {}

    today = dt.date.today().isoformat()
    urls = sorted(links)
    print(f"Checking {len(urls)} stream links ({WORKERS} at a time)")
    started = time.time()
    results = check_all(links, urls)

    retry = [url for url, (verdict, _) in results.items() if verdict == "dead"]
    if retry:
        print(f"  {len(retry)} didn't answer; trying those again in {RETRY_DELAY}s")
        time.sleep(RETRY_DELAY)
        results.update(check_all(links, retry))

    counts = {"ok": 0, "dead": 0, "blocked": 0}
    for url, (verdict, reason) in results.items():
        counts[verdict] += 1
        record = status.setdefault(url, {})
        record["checked"] = today
        record["last"] = verdict
        record["why"] = reason
        if verdict == "ok":
            record["fail_days"] = 0
            record["last_ok"] = today
        elif verdict == "dead" and record.get("last_fail_day") != today:
            record["fail_days"] = int(record.get("fail_days", 0)) + 1
            record["last_fail_day"] = today
        record["dead"] = int(record.get("fail_days", 0)) >= DEAD_AFTER_DAYS

    # Forget links that are no longer in the playlist.
    status = {url: record for url, record in status.items() if url in links}
    STATUS_FILE.write_text(json.dumps(status, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    confirmed = sum(1 for record in status.values() if record.get("dead"))
    print(
        f"Done in {time.time() - started:.0f}s: {counts['ok']} working, "
        f"{counts['dead']} not answering, {counts['blocked']} blocked (not counted)"
    )
    print(f"Links treated as dead (failed {DEAD_AFTER_DAYS}+ days in a row): {confirmed}")


if __name__ == "__main__":
    main()
