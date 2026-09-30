#!/usr/bin/env python3
"""Build epg.json: a compact programme guide index for the channels in playlist.m3u.

Downloads XMLTV guides from epgshare01, matches their channels against the
channels already in the playlist, and keeps only the upcoming programmes for
channels we actually carry.

Which guide files to use is discovered from the epgshare01 file list every run
(by name group, e.g. "US" or "US_LOCALS"), because the site renumbers files
from time to time (US1 became US2, CA1 disappeared). A fixed fallback list is
used if the file list can't be read.

Matching happens in tiers, best first, and each channel keeps the single best
guide it found:
  0. Local stations matched on their call letters (KNTV, WCAU, CBLT...) and
     the full tvg-id (e.g. "Telemundo.us@West").
  1. The channel's own name or network + feed ("Telemundo West").
  2. The bare network name ("Telemundo"). Never used for local stations, whose
     schedule differs from the national feed.

Output shape:
{
  "generated": 1754900000,
  "sources": [...],
  "stats": {...},
  "channels": {"CP24.ca@SD": [{"s": 1754900000, "e": 1754903600, "t": "...", "d": "..."}]}
}
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

PLAYLIST = Path("playlist.m3u")
OUTPUT = Path("epg.json")

EPG_INDEX = "https://epgshare01.online/epgshare01/"
EPG_FILE_RE = re.compile(r"epg_ripper_([A-Za-z_]+?)(\d+)\.xml\.gz")

# Guide groups to use, in order of preference when two guides match equally
# well. US_LOCALS is large (~55 MB) so it goes last.
WANTED_GROUPS = [
    "CA",
    "US",
    "US_SPORTS",
    "CL",
    "BEIN",
    "PLEX",
    "DISTROTV",
    "FANDUEL",
    "US_LOCALS",
]
FALLBACK_SOURCES = [
    "https://epgshare01.online/epgshare01/epg_ripper_CA2.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_US2.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_US_SPORTS1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_CL1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_PLEX1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_US_LOCALS1.xml.gz",
]

# How far ahead to keep, and the hard cap per channel (keeps epg.json small).
HOURS_AHEAD = 30
MAX_PROGRAMMES_PER_CHANNEL = 40
# Keep programmes that started up to this long ago so "now playing" survives.
HOURS_BEHIND = 4

REQUEST_TIMEOUT = 300
USER_AGENT = "Mozilla/5.0 (compatible; iptv-clean/1.0; +https://github.com/sizlackin/iptv-clean)"

ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')
XMLTV_TIME_RE = re.compile(r"^(\d{14})(?:\s*([+-]\d{4}))?")

# Feed codes that are local-station call letters: KNTV, WCAU, KMEXDT, CBLTDT.
CALLSIGN_FEED_RE = re.compile(r"^([CKW][A-Z]{2,4}?)(?:DT|TV|CD|LD)?$")
NOT_CALLSIGNS = {"WEST", "WESTHD", "CANADA", "CA", "KIDS", "CLASSIC", "WORLD", "CENTRAL"}
# Call letters inside guide names/ids: "KNTV", "WCAU-DT", "CBLT DT2".
CALLSIGN_TOKEN_RE = re.compile(r"(?<![A-Z0-9])([CKW][A-Z]{2,4})(?:[- ]?(?:DT|TV|CD|LD|HD)\d*)?(?![A-Z])")
# epgshare01 ends ids with the file they came from: ".us", ".ca2", ".us_locals1".
FILE_SUFFIX_RE = re.compile(r"\.[a-z][a-z0-9_]{1,14}$")
PAREN_RE = re.compile(r"\s*\(([^)]*)\)\s*")
COUNTRY_WORDS = {
    "chile": "cl", "us": "us", "usa": "us", "united states": "us", "eeuu": "us",
    "canada": "ca", "mexico": "mx", "méxico": "mx",
}
# A guide entry that is ONLY call letters ("KNTV-DT") is the station's main
# channel. Only these networks are ever a station's main channel in our list;
# the rest (Roar, MeTV, Telemundo on an NBC station...) are side channels.
MAIN_CHANNEL_NETWORKS = {
    "nbc", "cbs", "abc", "fox", "pbs", "cbctelevision", "ctv",
    "iciradiocanadatele", "tva", "ntv", "univision",
}
SUBCHANNEL_RE = re.compile(r"[A-Z]{3,5}[- ]?(?:DT|TV|CD|LD)[2-9]")


def normalise(value: str) -> str:
    """Lowercase alphanumeric key used for fuzzy channel matching."""
    return re.sub(r"[^a-z0-9]", "", (value or "").casefold())


def split_camel(value: str) -> str:
    """'MovieSphereGold' -> 'Movie Sphere Gold' (only used to build keys)."""
    value = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)
    return re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", value)


def network_word(network: str) -> str:
    """Short brand used to confirm a call-letter match: 'CBCTelevision' -> 'cbc'."""
    words = split_camel(network).split()
    if not words:
        return normalise(network)
    first = words[0]
    # Keep short all-caps brands (NBC, CBC, MNT); otherwise the first word
    # must be long enough not to hit other names ("Uni" would match Univision).
    if (first.isupper() and len(first) >= 3) or len(first) >= 5:
        return normalise(first)
    return normalise(network)


def feed_callsign(feed: str) -> str | None:
    code = feed.upper()
    if code in NOT_CALLSIGNS:
        return None
    match = CALLSIGN_FEED_RE.match(code)
    return match.group(1) if match else None


class PlaylistIndex:
    """Lookup tables from guide keys to the playlist tvg-ids they may match."""

    def __init__(self) -> None:
        self.tvg_ids: set[str] = set()
        # normalised key -> {tvg_id: tier}
        self.keys: dict[str, dict[str, int]] = {}
        # callsign -> {tvg_id: normalised network key}
        self.callsigns: dict[str, dict[str, str]] = {}
        # tvg_id -> country code (".us" -> "us"), and whether it can be a
        # station's main channel.
        self.country: dict[str, str] = {}
        self.main_channel: set[str] = set()

    def add_key(self, key: str, tvg_id: str, tier: int) -> None:
        key = normalise(key)
        if not key:
            return
        bucket = self.keys.setdefault(key, {})
        if tier < bucket.get(tvg_id, 99):
            bucket[tvg_id] = tier

    def add_channel(self, tvg_id: str, name: str) -> None:
        self.tvg_ids.add(tvg_id)
        bare, _, feed = tvg_id.partition("@")
        network = bare.rsplit(".", 1)[0] if "." in bare else bare
        country = bare.rsplit(".", 1)[1] if "." in bare else ""
        callsign = feed_callsign(feed) if feed else None

        self.country[tvg_id] = country.casefold()
        self.add_key(tvg_id, tvg_id, 0)
        if callsign:
            if normalise(network) in MAIN_CHANNEL_NETWORKS:
                self.main_channel.add(tvg_id)
            self.callsigns.setdefault(callsign, {})[tvg_id] = network_word(network)
            # "NBC WBAL-TV" style names are specific enough on their own.
            self.add_key(name, tvg_id, 1)
            return

        plain_feed = feed.upper() in ("", "SD", "HD")
        for text in (name, f"{network}{feed}", f"{network}{feed}{country}",
                     split_camel(network) + feed):
            self.add_key(text, tvg_id, 1)
        generic_tier = 1 if plain_feed else 2
        for text in (bare, network, split_camel(network)):
            self.add_key(text, tvg_id, generic_tier)


def load_playlist() -> PlaylistIndex:
    if not PLAYLIST.exists():
        raise SystemExit(f"Missing {PLAYLIST}. Run cleaner.py first.")
    index = PlaylistIndex()
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
        name = (attrs.get("tvg-name") or visible or "").strip()
        index.add_channel(tvg_id, name)
    return index


def name_variants(text: str) -> list[tuple[str, str | None]]:
    """Ways a guide might spell a name, each with the country it requires.

    "Canal.Mega.(Chile).cl" -> ("Canal Mega (Chile)", None), ("Canal Mega", "cl"),
    ("Mega", "cl").
    """
    base = FILE_SUFFIX_RE.sub("", text.strip()).replace(".", " ")
    variants: list[tuple[str, str | None]] = [(text, None), (base, None)]
    country = None
    match = PAREN_RE.search(base)
    if match:
        country = COUNTRY_WORDS.get(match.group(1).strip().casefold())
        if country:
            base = PAREN_RE.sub(" ", base).strip()
            variants.append((base, country))
    for prefix in ("canal ", "el canal "):
        if base.casefold().startswith(prefix):
            variants.append((base[len(prefix):], country))
    for suffix in (" hd", " sd"):
        if base.casefold().endswith(suffix):
            variants.append((base[: -len(suffix)], country))
    return variants


def match_guide_channel(index: PlaylistIndex, xmltv_id: str, names: list[str]) -> dict[str, int]:
    """Return {tvg_id: tier} for everything this guide channel could be."""
    found: dict[str, int] = {}

    def offer(tvg_id: str, tier: int) -> None:
        if tier < found.get(tvg_id, 99):
            found[tvg_id] = tier

    texts = [xmltv_id, *names]
    for text in texts:
        for variant, country in name_variants(text):
            for tvg_id, tier in index.keys.get(normalise(variant), {}).items():
                if country is None or index.country.get(tvg_id) == country:
                    offer(tvg_id, tier)

    # Local stations by call letters. If the guide names the network too
    # ("FOX (WCTI-TV2)") it must be ours; if it's ONLY call letters
    # ("KNTV-DT") it's the station's main channel, so it only goes to a
    # main-channel network (never Roar or MeTV on the same station).
    joined = normalise(" ".join(texts))
    upper_texts = " ".join(texts).upper()
    is_subchannel = bool(SUBCHANNEL_RE.search(upper_texts))
    for text in texts:
        for token in CALLSIGN_TOKEN_RE.findall(text.upper()):
            stations = index.callsigns.get(token)
            if not stations:
                continue
            names_a_network = any(key and key in joined for key in stations.values())
            for tvg_id, network_key in stations.items():
                if network_key and network_key in joined:
                    offer(tvg_id, 0)
                elif not names_a_network and not is_subchannel and tvg_id in index.main_channel:
                    offer(tvg_id, 1)
    return found


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


def discover_sources() -> list[str]:
    request = urllib.request.Request(EPG_INDEX, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            listing = response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        print(f"  ! could not read the guide file list ({exc}); using fallback list")
        return list(FALLBACK_SOURCES)

    by_group: dict[str, list[str]] = {}
    for match in EPG_FILE_RE.finditer(listing):
        group = match.group(1).rstrip("_").upper()
        by_group.setdefault(group, [])
        name = match.group(0)
        if name not in by_group[group]:
            by_group[group].append(name)

    sources: list[str] = []
    for group in WANTED_GROUPS:
        for name in sorted(by_group.get(group, [])):
            sources.append(EPG_INDEX + name)
    if not sources:
        print("  ! guide file list had none of the wanted files; using fallback list")
        return list(FALLBACK_SOURCES)
    return sources


def open_guide(url: str):
    """Open a (possibly gzipped) guide as a stream, so big files never sit in memory."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    response = urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT)
    if url.endswith(".gz"):
        return gzip.GzipFile(fileobj=response)
    return response


# A few guide channel names per file go into the stats, so it's easy to see
# how a file names its channels when it stops matching.
SAMPLE_FIRST = 8
SAMPLE_WORDS = ("mega", "kntv", "wcau", "nbc", "telemundo", "univision", "golazo", "tudn", "bein", "fox")
SAMPLE_MAX = 30


def record_sample(samples: list[list[object]], xmltv_id: str, names: list[str]) -> None:
    if len(samples) >= SAMPLE_MAX:
        return
    text = f"{xmltv_id} {' '.join(names)}".casefold()
    if len(samples) < SAMPLE_FIRST or any(word in text for word in SAMPLE_WORDS):
        samples.append([xmltv_id, names[:3]])


def harvest(
    stream,
    source_rank: int,
    index: PlaylistIndex,
    window_start: int,
    window_end: int,
    candidates: dict[str, tuple[tuple[int, int, int], str]],
    programmes: dict[str, list[dict[str, object]]],
    samples: list[list[object]],
) -> tuple[int, int]:
    """Read one XMLTV document. Returns (guide channels matched, programmes kept)."""
    guide_to_tvg: dict[str, dict[str, int]] = {}
    samples.clear()
    order = 0
    matched = 0
    kept = 0

    for _, element in ET.iterparse(stream, events=("end",)):
        tag = element.tag.rsplit("}", 1)[-1]

        if tag == "channel":
            xmltv_id = (element.get("id") or "").strip()
            names = [
                (child.text or "").strip()
                for child in element
                if child.tag.rsplit("}", 1)[-1] == "display-name"
            ]
            element.clear()
            if not xmltv_id:
                continue
            record_sample(samples, xmltv_id, names)
            found = match_guide_channel(index, xmltv_id, names)
            if not found:
                continue
            order += 1
            matched += 1
            guide_key = f"{source_rank}|{xmltv_id}"
            guide_to_tvg[xmltv_id] = found
            for tvg_id, tier in found.items():
                score = (tier, source_rank, order)
                best = candidates.get(tvg_id)
                if best is None or score < best[0]:
                    candidates[tvg_id] = (score, guide_key)
            continue

        if tag != "programme":
            continue

        xmltv_id = (element.get("channel") or "").strip()
        if xmltv_id not in guide_to_tvg:
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
        programmes.setdefault(f"{source_rank}|{xmltv_id}", []).append(entry)
        kept += 1

    return matched, kept


def main() -> None:
    index = load_playlist()
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    window_start = now - HOURS_BEHIND * 3600
    window_end = now + HOURS_AHEAD * 3600

    # Best guide channel per playlist channel, and programmes per guide channel.
    candidates: dict[str, tuple[tuple[int, int, int], str]] = {}
    programmes: dict[str, list[dict[str, object]]] = {}
    used_sources: list[str] = []
    source_stats: list[dict[str, object]] = []

    print(f"Playlist channels with a tvg-id: {len(index.tvg_ids)}")
    sources = discover_sources()
    for rank, url in enumerate(sources):
        print(f"Fetching {url}")
        try:
            samples: list[list[object]] = []
            with open_guide(url) as stream:
                matched, kept = harvest(
                    stream, rank, index, window_start, window_end, candidates, programmes, samples
                )
        except (urllib.error.URLError, OSError, TimeoutError, EOFError) as exc:
            print(f"  ! skipped ({exc})")
            source_stats.append({"url": url, "error": str(exc)[:200]})
            continue
        except ET.ParseError as exc:
            print(f"  ! malformed XML, skipped ({exc})")
            source_stats.append({"url": url, "error": f"bad XML: {exc}"[:200]})
            continue
        print(f"  matched {matched} guide channels, kept {kept} programmes")
        used_sources.append(url)
        entry = {"url": url, "matched": matched, "programmes": kept}
        if matched < 40:
            entry["sample_channels"] = samples
        source_stats.append(entry)

    collected: dict[str, list[dict[str, object]]] = {}
    tiers = {0: 0, 1: 0, 2: 0}
    for tvg_id, (score, guide_key) in candidates.items():
        entries = sorted(programmes.get(guide_key, []), key=lambda p: p["s"])
        deduped: list[dict[str, object]] = []
        seen: set[tuple[object, object]] = set()
        for programme in entries:
            marker = (programme["s"], programme["t"])
            if marker not in seen:
                seen.add(marker)
                deduped.append(programme)
        if deduped:
            collected[tvg_id] = deduped[:MAX_PROGRAMMES_PER_CHANNEL]
            tiers[score[0]] += 1

    stats = {
        "channels": len(index.tvg_ids),
        "with_guide": len(collected),
        "by_match_type": {"call_letters_or_exact_id": tiers[0], "name": tiers[1], "network": tiers[2]},
        "sources": source_stats,
    }
    payload = {
        "generated": now,
        "sources": used_sources,
        "stats": stats,
        "channels": collected,
    }
    OUTPUT.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    total = len(index.tvg_ids)
    coverage = (len(collected) / total * 100) if total else 0
    print(
        f"Wrote {OUTPUT}: {len(collected)}/{total} channels have a guide "
        f"({coverage:.1f}%), {OUTPUT.stat().st_size / 1024:.0f} KB"
    )
    print(f"  match types: {stats['by_match_type']}")
    if not collected:
        print("WARNING: no guide data matched. The addon will build without EPG.")


if __name__ == "__main__":
    main()
