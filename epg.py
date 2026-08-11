#!/usr/bin/env python3
"""Build epg.json: a compact programme guide index for the channels in playlist.m3u.

Downloads one or more XMLTV guides, matches their channels against the tvg-id /
tvg-name values already in the playlist, and keeps only the upcoming programmes
for channels we actually carry.

Matching is deliberately fuzzy (normalised id + every <display-name>) because
public XMLTV feeds do not all use iptv-org channel ids. The build log prints a
match rate so it is obvious when a source stops lining up.

Output shape:
{
  "generated": 1754900000,
  "timezone": "America/Toronto",
  "sources": [...],
  "channels": {"CP24.ca": [{"s": 1754900000, "e": 1754903600, "t": "...", "d": "..."}]}
}
"""

from __future__ import annotations

import datetime as dt
import gzip
import io
import json
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

PLAYLIST = Path("playlist.m3u")
OUTPUT = Path("epg.json")

# Any source that 404s, times out or returns junk is skipped with a warning —
# the addon build still succeeds, just without guide data from that source.
EPG_SOURCES = [
    "https://epgshare01.online/epgshare01/epg_ripper_CA1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_CA2.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_US1.xml.gz",
]

# How far ahead to keep, and the hard cap per channel (keeps epg.json small).
HOURS_AHEAD = 30
MAX_PROGRAMMES_PER_CHANNEL = 40
# Keep programmes that started up to this long ago so "now playing" survives.
HOURS_BEHIND = 4

REQUEST_TIMEOUT = 180
USER_AGENT = "Mozilla/5.0 (compatible; iptv-clean/1.0; +https://github.com/sizlackin/iptv-clean)"

ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')
XMLTV_TIME_RE = re.compile(r"^(\d{14})(?:\s*([+-]\d{4}))?")


def normalise(value: str) -> str:
    """Lowercase alphanumeric key used for fuzzy channel matching."""
    return re.sub(r"[^a-z0-9]", "", (value or "").casefold())


def playlist_keys() -> dict[str, set[str]]:
    """Map each playlist tvg-id to the set of normalised keys it may match on."""
    if not PLAYLIST.exists():
        raise SystemExit(f"Missing {PLAYLIST}. Run cleaner.py first.")

    keys: dict[str, set[str]] = {}
    for line in PLAYLIST.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if not line.startswith("#EXTINF:"):
            continue
        prefix, sep, visible = line.rpartition(",")
        if not sep:
            continue
        attrs = {m.group(1): m.group(2) for m in ATTR_RE.finditer(prefix)}
        tvg_id = (attrs.get("tvg-id") or "").strip()
        if not tvg_id:
            continue
        bare = tvg_id.split("@", 1)[0]
        name = (attrs.get("tvg-name") or visible or "").strip()

        candidates = {tvg_id, bare, name}
        # "CP24.ca" should also match a guide that just calls it "CP24".
        if "." in bare:
            candidates.add(bare.rsplit(".", 1)[0])
        keys.setdefault(tvg_id, set()).update(
            normalise(c) for c in candidates if normalise(c)
        )
    return keys


def parse_xmltv_time(value: str) -> int | None:
    match = XMLTV_TIME_RE.match((value or "").strip())
    if not match:
        return None
    stamp, offset = match.groups()
    try:
        naive = dt.datetime.strptime(stamp, "%Y%m%d%H%M%S")
    except ValueError:
        return None
    if offset:
        sign = 1 if offset[0] == "+" else -1
        delta = dt.timedelta(hours=int(offset[1:3]), minutes=int(offset[3:5]))
        aware = naive.replace(tzinfo=dt.timezone(sign * delta))
    else:
        aware = naive.replace(tzinfo=dt.timezone.utc)
    return int(aware.timestamp())


def fetch(url: str) -> bytes | None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            payload = response.read()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as exc:
        print(f"  ! skipped {url} ({exc})")
        return None

    if url.endswith(".gz") or payload[:2] == b"\x1f\x8b":
        try:
            payload = gzip.decompress(payload)
        except OSError as exc:
            print(f"  ! could not decompress {url} ({exc})")
            return None
    return payload


def harvest(
    payload: bytes,
    key_index: dict[str, str],
    window_start: int,
    window_end: int,
    collected: dict[str, list[dict[str, object]]],
) -> tuple[int, int]:
    """Stream one XMLTV document, returning (channels matched, programmes kept)."""
    xmltv_to_tvg: dict[str, str] = {}
    matched = 0
    kept = 0

    stream = io.BytesIO(payload)
    for event, element in ET.iterparse(stream, events=("end",)):
        tag = element.tag.rsplit("}", 1)[-1]

        if tag == "channel":
            xmltv_id = (element.get("id") or "").strip()
            candidates = [xmltv_id]
            candidates += [
                (child.text or "")
                for child in element
                if child.tag.rsplit("}", 1)[-1] == "display-name"
            ]
            for candidate in candidates:
                target = key_index.get(normalise(candidate))
                if target:
                    if xmltv_id and xmltv_id not in xmltv_to_tvg:
                        xmltv_to_tvg[xmltv_id] = target
                        matched += 1
                    break
            element.clear()
            continue

        if tag != "programme":
            continue

        target = xmltv_to_tvg.get((element.get("channel") or "").strip())
        if not target:
            element.clear()
            continue

        start = parse_xmltv_time(element.get("start") or "")
        stop = parse_xmltv_time(element.get("stop") or "")
        if start is None or start > window_end or (stop is not None and stop < window_start):
            element.clear()
            continue

        title = ""
        description = ""
        for child in element:
            child_tag = child.tag.rsplit("}", 1)[-1]
            if child_tag == "title" and not title:
                title = (child.text or "").strip()
            elif child_tag == "desc" and not description:
                description = (child.text or "").strip()
        element.clear()

        if not title:
            continue

        entry: dict[str, object] = {"s": start, "t": title[:160]}
        if stop is not None:
            entry["e"] = stop
        if description:
            entry["d"] = description[:400]
        collected.setdefault(target, []).append(entry)
        kept += 1

    return matched, kept


def main() -> None:
    keys = playlist_keys()

    # Reverse index: normalised key -> tvg-id. First writer wins, so more
    # specific ids registered earlier are not clobbered by generic names.
    key_index: dict[str, str] = {}
    for tvg_id, candidates in keys.items():
        for candidate in candidates:
            key_index.setdefault(candidate, tvg_id)

    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    window_start = now - HOURS_BEHIND * 3600
    window_end = now + HOURS_AHEAD * 3600

    collected: dict[str, list[dict[str, object]]] = {}
    used_sources: list[str] = []

    print(f"Playlist channels with a tvg-id: {len(keys)}")
    for url in EPG_SOURCES:
        print(f"Fetching {url}")
        payload = fetch(url)
        if not payload:
            continue
        try:
            matched, kept = harvest(payload, key_index, window_start, window_end, collected)
        except ET.ParseError as exc:
            print(f"  ! malformed XML, skipped ({exc})")
            continue
        print(f"  matched {matched} channels, kept {kept} programmes")
        used_sources.append(url)

    for tvg_id, programmes in collected.items():
        programmes.sort(key=lambda p: p["s"])
        deduped: list[dict[str, object]] = []
        seen: set[tuple[object, object]] = set()
        for programme in programmes:
            marker = (programme["s"], programme["t"])
            if marker in seen:
                continue
            seen.add(marker)
            deduped.append(programme)
        collected[tvg_id] = deduped[:MAX_PROGRAMMES_PER_CHANNEL]

    payload = {
        "generated": now,
        "sources": used_sources,
        "channels": collected,
    }
    OUTPUT.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    coverage = (len(collected) / len(keys) * 100) if keys else 0
    total = sum(len(v) for v in collected.values())
    print(
        f"Wrote {OUTPUT}: {len(collected)}/{len(keys)} channels have a guide "
        f"({coverage:.1f}%), {total} programmes, {OUTPUT.stat().st_size / 1024:.0f} KB"
    )
    if not collected:
        print("WARNING: no guide data matched. The addon will build without EPG.")


if __name__ == "__main__":
    main()
