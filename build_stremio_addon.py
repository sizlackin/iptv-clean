#!/usr/bin/env python3
"""Build a static Stremio live-TV addon from the cleaned playlist.m3u.

The output in site/ is suitable for GitHub Pages. Channels with the same tvg-id
are collapsed into one Stremio card while retaining all unique stream URLs.

Metadata (proper names, network, owner, launch year, categories, feed quality)
comes from the authoritative iptv-org database rather than name guessing.
Programme data comes from epg.json if epg.py has been run first.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import html
import io
import json
import re
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

PLAYLIST = Path("playlist.m3u")
EPG_FILE = Path("epg.json")
SITE_DIR = Path("site")
SITE_BASE = "https://sizlackin.github.io/iptv-clean"
ADDON_ID_PREFIX = "iptv_"
ADDON_VERSION = "1.2.0"

# Clock times shown in channel descriptions are rendered in this zone.
DISPLAY_TIMEZONE = "America/Toronto"

DB_BASE = "https://raw.githubusercontent.com/iptv-org/database/master/data"
CHANNELS_CSV = f"{DB_BASE}/channels.csv"
FEEDS_CSV = f"{DB_BASE}/feeds.csv"
DB_TIMEOUT = 60
USER_AGENT = "Mozilla/5.0 (compatible; iptv-clean/1.0; +https://github.com/sizlackin/iptv-clean)"

# How many upcoming programmes to list in a channel description.
GUIDE_ENTRIES = 8

# Order matters: this is the order the genre chips appear in Stremio.
GENRE_FILTERS = [
    "Popular",
    "Canada",
    "USA",
    "Sports",
    "News",
    "Movies",
    "Series",
    "Kids",
    "Entertainment",
    "Comedy",
    "Music",
    "Documentary",
    "Lifestyle",
    "Religious",
    "Government",
    "Other",
]

# iptv-org category token -> our display genre.
CATEGORY_MAP = {
    "sports": "Sports",
    "news": "News",
    "weather": "News",
    "business": "News",
    "movies": "Movies",
    "classic": "Movies",
    "series": "Series",
    "kids": "Kids",
    "animation": "Kids",
    "family": "Kids",
    "entertainment": "Entertainment",
    "general": "Entertainment",
    "comedy": "Comedy",
    "music": "Music",
    "documentary": "Documentary",
    "science": "Documentary",
    "education": "Documentary",
    "culture": "Documentary",
    "lifestyle": "Lifestyle",
    "cooking": "Lifestyle",
    "travel": "Lifestyle",
    "outdoor": "Lifestyle",
    "auto": "Lifestyle",
    "relax": "Lifestyle",
    "shop": "Lifestyle",
    "religious": "Religious",
    "legislative": "Government",
}

# Only used when neither group-title nor the iptv-org record give a category.
NAME_FALLBACK = [
    ("Sports", ("sport", "nfl", "nba", "nhl", "mlb", "soccer", "football", "hockey", "golf", "ufc", "racing")),
    ("News", ("news", "cp24", "weather", "meteo", "bloomberg", "cnbc")),
    ("Kids", ("kids", "junior", "cartoon", "nick", "disney", "boomerang", "toon")),
    ("Movies", ("movie", "cinema", "cine", "film")),
    ("Music", ("music", "mtv", "vevo", "radio", "hits")),
    ("Religious", ("church", "faith", "gospel", "bible", "catholic", "christ", "ministry")),
    ("Documentary", ("documentary", "discovery", "history", "nature", "science", "nat geo")),
    ("Comedy", ("comedy", "laugh")),
]

LANGUAGE_NAMES = {
    "eng": "English",
    "fra": "French",
    "fre": "French",
    "spa": "Spanish",
    "cmn": "Mandarin",
    "yue": "Cantonese",
    "por": "Portuguese",
    "ita": "Italian",
    "deu": "German",
    "pan": "Punjabi",
    "hin": "Hindi",
    "ara": "Arabic",
    "kor": "Korean",
    "vie": "Vietnamese",
    "tgl": "Tagalog",
    "ukr": "Ukrainian",
    "pol": "Polish",
    "rus": "Russian",
    "ell": "Greek",
    "iku": "Inuktitut",
    "cre": "Cree",
}

# Ranked "most popular" shortlist, matched on the bare tvg-id. The list is
# longer than POPULAR_LIMIT so the row backfills if iptv-org drops a channel.
POPULAR_RANKING = [
    "cbctdt.ca",
    "cbcnewsnetwork.ca",
    "ctv2atlantic.ca",
    "citynewstoronto.ca",
    "cp24.ca",
    "tsn1.ca",
    "bnnbloomberg.ca",
    "nbc.us",
    "abc.us",
    "cbs.us",
    "fox.us",
    "foxnewschannel.us",
    "espnu.us",
    "espnews.us",
    "foxsports1.us",
    "nflnetwork.us",
    "nhlnetwork.us",
    "mlbnetwork.us",
    "cnbc.us",
    "pbs.us",
    "amc.us",
    "history.us",
    "nickelodeon.us",
    "disneychannel.us",
    "syfy.us",
    "bravo.us",
    "cpacenglish.ca",
    "icirdi.ca",
    "tsntheocho.ca",
    "citynewsvancouver.ca",
    "citynewscalgary.ca",
    "ctvlifechannel.ca",
    "abcnewslive1.us",
    "nbcnewsnow.us",
    "cbsnews247.us",
    "espndeportes.us",
    "foxsports2.us",
    "cheddarnews.us",
]
POPULAR_LIMIT = 15

ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')
CALLSIGN_RE = re.compile(r"^[A-Z]{3,5}(?:-(?:DT|TV|LD|CD|CA|FM))?\d*$")
# A local station whose name starts with a callsign, e.g. "WXII-TV 20.1".
STATION_RE = re.compile(r"^[A-Z]{3,5}(?:-(?:DT|TV|LD|CD|CA|FM))?\b")


@dataclass
class StreamEntry:
    url: str
    referrer: str | None = None
    user_agent: str | None = None


@dataclass
class Channel:
    id: str
    source_key: str
    tvg_id: str | None
    name: str
    poster: str | None
    group: str | None
    country: str
    streams: list[StreamEntry] = field(default_factory=list)
    # Filled in by enrich().
    display_name: str = ""
    record: dict[str, str] = field(default_factory=dict)
    feed: dict[str, str] = field(default_factory=dict)

    @property
    def bare_id(self) -> str:
        return (self.tvg_id or "").split("@", 1)[0].casefold()

    @property
    def feed_code(self) -> str:
        parts = (self.tvg_id or "").split("@", 1)
        return parts[1] if len(parts) == 2 else ""


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def parse_attrs(extinf_prefix: str) -> dict[str, str]:
    return {match.group(1): match.group(2) for match in ATTR_RE.finditer(extinf_prefix)}


def stable_channel_id(source_key: str) -> str:
    digest = hashlib.sha1(source_key.encode("utf-8")).hexdigest()[:20]
    return f"{ADDON_ID_PREFIX}{digest}"


def country_for(tvg_id: str | None, name: str) -> str:
    if tvg_id:
        bare = tvg_id.split("@", 1)[0].lower()
        if bare.endswith(".ca"):
            return "Canada"
        if bare.endswith(".us"):
            return "USA"
    lower_name = name.lower()
    if "canada" in lower_name or "canadian" in lower_name:
        return "Canada"
    return "Unknown"


def parse_playlist(path: Path) -> list[Channel]:
    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    channels: dict[str, Channel] = {}

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line.startswith("#EXTINF:"):
            i += 1
            continue

        prefix, sep, visible_name = line.rpartition(",")
        if not sep:
            i += 1
            continue

        attrs = parse_attrs(prefix)
        name = (attrs.get("tvg-name") or visible_name or "Live TV").strip()
        tvg_id = (attrs.get("tvg-id") or "").strip() or None
        poster = (attrs.get("tvg-logo") or "").strip() or None
        group = (attrs.get("group-title") or "").strip() or None

        referrer = None
        user_agent = None
        stream_url = None
        j = i + 1
        while j < len(lines) and not lines[j].startswith("#EXTINF:"):
            candidate = lines[j].strip()
            lower = candidate.lower()
            if lower.startswith("#extvlcopt:http-referrer="):
                referrer = candidate.split("=", 1)[1].strip()
            elif lower.startswith("#extvlcopt:http-user-agent="):
                user_agent = candidate.split("=", 1)[1].strip()
            elif candidate and not candidate.startswith("#"):
                stream_url = candidate
                break
            j += 1

        if not stream_url:
            i = max(i + 1, j)
            continue

        source_key = tvg_id or f"{name}|{group or ''}"
        channel = channels.get(source_key)
        if channel is None:
            channel = Channel(
                id=stable_channel_id(source_key),
                source_key=source_key,
                tvg_id=tvg_id,
                name=name,
                poster=poster,
                group=group,
                country=country_for(tvg_id, name),
            )
            channels[source_key] = channel
        else:
            if not channel.poster and poster:
                channel.poster = poster
            if not channel.group and group:
                channel.group = group

        if all(existing.url != stream_url for existing in channel.streams):
            channel.streams.append(
                StreamEntry(stream_url, referrer=referrer, user_agent=user_agent)
            )
        i = max(i + 1, j)

    return sorted(channels.values(), key=lambda c: c.name.casefold())


# --------------------------------------------------------------------------
# iptv-org database metadata
# --------------------------------------------------------------------------


def fetch_csv(url: str) -> list[dict[str, str]]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=DB_TIMEOUT) as response:
            payload = response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as exc:
        print(f"  ! could not load {url} ({exc}) — continuing without it")
        return []
    return list(csv.DictReader(io.StringIO(payload)))


def load_database() -> tuple[dict[str, dict[str, str]], dict[tuple[str, str], dict[str, str]]]:
    print("Loading iptv-org metadata")
    channels = {row["id"]: row for row in fetch_csv(CHANNELS_CSV) if row.get("id")}
    feeds: dict[tuple[str, str], dict[str, str]] = {}
    for row in fetch_csv(FEEDS_CSV):
        channel = row.get("channel")
        feed_id = row.get("id")
        if channel and feed_id:
            feeds[(channel, feed_id)] = row
    print(f"  {len(channels)} channel records, {len(feeds)} feed records")
    return channels, feeds


def enrich(
    channels: list[Channel],
    records: dict[str, dict[str, str]],
    feeds: dict[tuple[str, str], dict[str, str]],
) -> int:
    """Attach database rows and compute a human display name. Returns hit count."""
    hits = 0
    for channel in channels:
        bare = (channel.tvg_id or "").split("@", 1)[0]
        record = records.get(bare, {})
        feed = feeds.get((bare, channel.feed_code), {})
        channel.record = record
        channel.feed = feed
        if record:
            hits += 1

        base = (record.get("name") or channel.name).strip()
        network = (record.get("network") or "").strip()
        base_key = re.sub(r"[^a-z0-9]", "", base.casefold())
        net_key = re.sub(r"[^a-z0-9]", "", network.casefold())

        # A bare callsign like "CBCT-DT" is useless in a search result, so lead
        # with the network the database knows about. But never restate a
        # network that is just a longer form of the name ("CBS Entertainment").
        if not network or not net_key or net_key.startswith(base_key):
            label = base
        elif CALLSIGN_RE.match(base):
            label = f"{network} {base}"
        elif net_key in base_key:
            label = base
        elif STATION_RE.match(base):
            # Local affiliates benefit from knowing whose network they carry.
            label = f"{base} ({network})"
        else:
            label = base

        feed_name = (feed.get("name") or "").strip()
        if feed_name and (feed.get("is_main") or "").upper() != "TRUE":
            if feed_name.casefold() not in label.casefold():
                label = f"{label} ({feed_name})"

        channel.display_name = label
    return hits


def category_labels(channel: Channel) -> list[str]:
    """Content categories, preferring authoritative iptv-org data."""
    labels: list[str] = []
    sources = [channel.group or "", channel.record.get("categories", "")]
    for raw in sources:
        for token in re.split(r"[;,/|]", raw):
            token = token.strip().casefold()
            if not token or token in {"undefined", "unknown", "other"}:
                continue
            mapped = CATEGORY_MAP.get(token)
            if mapped and mapped not in labels:
                labels.append(mapped)
        if labels:
            break

    if not labels:
        name = f"{channel.display_name} {channel.name}".casefold()
        for genre, keywords in NAME_FALLBACK:
            if any(keyword in name for keyword in keywords):
                labels.append(genre)
                break

    if not labels:
        labels.append("Other")
    return labels


def genre_memberships(channel: Channel, popular_ids: set[str]) -> set[str]:
    result: set[str] = set(category_labels(channel))
    if channel.country in ("Canada", "USA"):
        result.add(channel.country)
    if channel.id in popular_ids:
        result.add("Popular")
    return result


def pick_popular(channels: list[Channel]) -> list[Channel]:
    by_bare: dict[str, Channel] = {}
    for channel in channels:
        bare = channel.bare_id
        if not bare:
            continue
        current = by_bare.get(bare)
        if current is None or len(channel.streams) > len(current.streams):
            by_bare[bare] = channel

    picked: list[Channel] = []
    for bare in POPULAR_RANKING:
        channel = by_bare.get(bare)
        if channel is not None:
            picked.append(channel)
        if len(picked) >= POPULAR_LIMIT:
            break
    return picked


# --------------------------------------------------------------------------
# EPG
# --------------------------------------------------------------------------


def load_epg() -> tuple[dict[str, list[dict[str, object]]], int]:
    if not EPG_FILE.exists():
        print(f"No {EPG_FILE} found — building without programme data.")
        return {}, 0
    try:
        payload = json.loads(EPG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"! could not read {EPG_FILE} ({exc}) — building without programme data.")
        return {}, 0
    channels = payload.get("channels") or {}
    generated = int(payload.get("generated") or 0)
    print(f"Loaded guide data for {len(channels)} channels")
    return channels, generated


def local_zone():
    if ZoneInfo is None:
        return dt.timezone.utc
    try:
        return ZoneInfo(DISPLAY_TIMEZONE)
    except Exception:  # pragma: no cover - missing tzdata
        return dt.timezone.utc


def clock(epoch: object, zone) -> str:
    moment = dt.datetime.fromtimestamp(int(epoch), tz=zone)
    return moment.strftime("%a %-I:%M %p")


def guide_block(
    channel: Channel,
    epg: dict[str, list[dict[str, object]]],
    generated: int,
    zone,
) -> tuple[str, dict[str, object] | None]:
    """Returns (description block, current programme) for a channel."""
    programmes = epg.get(channel.tvg_id or "") or []
    if not programmes:
        return "", None

    reference = generated or int(dt.datetime.now(dt.timezone.utc).timestamp())
    current = None
    for programme in programmes:
        start = int(programme["s"])
        end = int(programme.get("e") or (start + 3600))
        if start <= reference < end:
            current = programme
            break

    upcoming = [p for p in programmes if int(p.get("e") or int(p["s"]) + 3600) > reference]
    lines = ["", "── GUIDE ──"]
    if current:
        lines.append(f"On now  {clock(current['s'], zone)}  {current['t']}")
        if current.get("d"):
            lines.append(f"  {current['d']}")
    for programme in upcoming[: GUIDE_ENTRIES + 1]:
        if current is not None and programme is current:
            continue
        lines.append(f"{clock(programme['s'], zone)}  {programme['t']}")
    if generated:
        lines.append(f"(guide as of {clock(generated, zone)})")
    return "\n".join(lines), current


# --------------------------------------------------------------------------
# Stremio documents
# --------------------------------------------------------------------------


def meta_preview(channel: Channel) -> dict[str, object]:
    meta: dict[str, object] = {
        "id": channel.id,
        "type": "tv",
        "name": channel.display_name or channel.name,
        "posterShape": "poster",
    }
    if channel.poster:
        meta["poster"] = channel.poster
    return meta


def full_meta(
    channel: Channel,
    popular_ids: set[str],
    epg: dict[str, list[dict[str, object]]],
    generated: int,
    zone,
) -> dict[str, object]:
    record = channel.record
    feed = channel.feed
    categories = category_labels(channel)

    header = ["Live TV"]
    if channel.country != "Unknown":
        header.append(channel.country)
    header.extend(categories)
    lines = [" • ".join(header)]

    owners = (record.get("owners") or "").replace(";", ", ")
    if owners:
        lines.append(f"Operated by {owners}")

    feed_bits = []
    feed_name = (feed.get("name") or "").strip()
    if feed_name:
        feed_bits.append(f"{feed_name} feed")
    quality = (feed.get("format") or "").strip()
    if quality:
        feed_bits.append(quality)
    languages = [
        LANGUAGE_NAMES.get(code.strip(), code.strip())
        for code in (feed.get("languages") or "").split(";")
        if code.strip()
    ]
    if languages:
        feed_bits.append("/".join(languages))
    if feed_bits:
        lines.append(" · ".join(feed_bits))

    launched = (record.get("launched") or "").strip()
    if len(launched) >= 4:
        lines.append(f"On air since {launched[:4]}")

    alt_names = (record.get("alt_names") or "").replace(";", ", ")
    if alt_names:
        lines.append(f"Also known as {alt_names}")

    guide_text, current = guide_block(channel, epg, generated, zone)
    if guide_text:
        lines.append(guide_text)
    else:
        lines.append("")
        lines.append("No programme guide available for this channel.")

    genres = [g for g in GENRE_FILTERS if g in genre_memberships(channel, popular_ids)]

    meta = meta_preview(channel)
    meta.update(
        {
            "description": "\n".join(lines).strip(),
            "releaseInfo": "LIVE",
            "genres": genres,
        }
    )
    if channel.poster:
        meta["background"] = channel.poster
    if channel.country != "Unknown":
        meta["country"] = channel.country
    if launched[:4].isdigit():
        meta["year"] = launched[:4]

    links: list[dict[str, str]] = [
        {"name": genre, "category": "Genres", "url": f"stremio:///discover/{genre}"}
        for genre in genres
    ]
    website = (record.get("website") or "").strip()
    if website:
        links.append({"name": "Official site", "category": "Links", "url": website})
    if links:
        meta["links"] = links

    if current:
        meta["tagline"] = str(current["t"])[:120]
    return meta


def stream_response(
    channel: Channel,
    epg: dict[str, list[dict[str, object]]],
    generated: int,
    zone,
) -> dict[str, object]:
    _, current = guide_block(channel, epg, generated, zone)
    now_line = f"Now: {current['t']}" if current else None

    quality = (channel.feed.get("format") or "").strip()
    feed_name = (channel.feed.get("name") or "").strip()

    streams: list[dict[str, object]] = []
    for index, entry in enumerate(channel.streams, start=1):
        parsed = urlparse(entry.url)
        host = parsed.hostname or "Live TV"
        behavior: dict[str, object] = {
            "notWebReady": True,
            "bingeGroup": "sizlackin-live-tv",
        }
        request_headers: dict[str, str] = {}
        if entry.user_agent:
            request_headers["User-Agent"] = entry.user_agent
        if entry.referrer:
            request_headers["Referer"] = entry.referrer
        if request_headers:
            behavior["proxyHeaders"] = {"request": request_headers}

        label_bits = [feed_name or "Live"]
        if quality:
            label_bits.append(quality)
        if len(channel.streams) > 1:
            label_bits.append(f"Source {index}")

        description_bits = [channel.display_name or channel.name]
        if now_line:
            description_bits.append(now_line)
        description_bits.append(host)

        streams.append(
            {
                "url": entry.url,
                "name": " • ".join(label_bits),
                "description": "\n".join(description_bits),
                "behaviorHints": behavior,
            }
        )
    return {"streams": streams}


def build_manifest() -> dict[str, object]:
    return {
        "id": "com.sizlackin.livetv",
        "version": ADDON_VERSION,
        "name": "Cesar Live TV",
        "description": "Clean Canada + USA live TV with iptv-org metadata and programme guide, rebuilt automatically.",
        "logo": f"{SITE_BASE}/addon-logo.svg",
        "resources": [
            "catalog",
            {"name": "meta", "types": ["tv"], "idPrefixes": [ADDON_ID_PREFIX]},
            {"name": "stream", "types": ["tv"], "idPrefixes": [ADDON_ID_PREFIX]},
        ],
        "types": ["tv"],
        "idPrefixes": [ADDON_ID_PREFIX],
        "catalogs": [
            {"type": "tv", "id": "popular", "name": "Popular Channels"},
            {
                "type": "tv",
                "id": "live-tv",
                "name": "Live TV",
                "genres": GENRE_FILTERS,
                "extra": [{"name": "genre", "options": GENRE_FILTERS, "isRequired": False}],
            },
        ],
    }


def build_index(
    channel_count: int,
    stream_count: int,
    popular: list[Channel],
    guide_count: int,
    generated: int,
    zone,
) -> str:
    manifest_url = f"{SITE_BASE}/manifest.json"
    stremio_url = manifest_url.replace("https://", "stremio://", 1)
    chips = "".join(
        f'<span class="chip">{html.escape(c.display_name or c.name)}</span>' for c in popular
    )
    stamp = clock(generated, zone) if generated else "not built yet"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cesar Live TV</title>
<style>
:root{{color-scheme:dark;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}body{{margin:0;background:#0f1115;color:#f4f5f7;display:grid;min-height:100vh;place-items:center}}main{{width:min(680px,calc(100% - 36px));background:#171a21;border:1px solid #2a2f3a;border-radius:24px;padding:28px;box-sizing:border-box;margin:24px 0}}.dot{{display:inline-block;width:10px;height:10px;border-radius:50%;background:#1ed760;box-shadow:0 0 14px #1ed76088;margin-right:8px}}h1{{margin:.25rem 0 1rem;font-size:2rem}}h2{{font-size:.8rem;letter-spacing:.08em;text-transform:uppercase;color:#8d95a3;margin:22px 0 10px}}p{{color:#b8bec9;line-height:1.5}}.stats{{display:flex;gap:12px;flex-wrap:wrap;margin:20px 0}}.stat{{background:#101217;border:1px solid #292e38;border-radius:14px;padding:12px 16px}}.stat b{{display:block;font-size:1.25rem;color:white}}.chips{{display:flex;gap:8px;flex-wrap:wrap}}.chip{{background:#101217;border:1px solid #292e38;border-radius:999px;padding:6px 12px;font-size:.85rem;color:#cfd5df}}a.button{{display:block;text-align:center;background:#1ed760;color:#06130a;text-decoration:none;font-weight:800;padding:15px 18px;border-radius:999px;margin-top:22px}}code{{display:block;background:#0d0f13;border-radius:12px;padding:12px;overflow-wrap:anywhere;color:#dfe4ec}}small{{color:#767e8c}}</style></head>
<body><main><div><span class="dot"></span>GitHub-powered</div><h1>Cesar Live TV</h1><p>Your custom Stremio live-TV addon. It is rebuilt automatically from your cleaned Canada + USA IPTV playlist.</p><div class="stats"><div class="stat"><b>{channel_count}</b>channels</div><div class="stat"><b>{stream_count}</b>streams</div><div class="stat"><b>{guide_count}</b>with guide</div></div><h2>Popular row</h2><div class="chips">{chips}</div><a class="button" href="{html.escape(stremio_url)}">Install in Stremio</a><p>Manual addon URL:</p><code>{html.escape(manifest_url)}</code><p><small>Guide last built {html.escape(stamp)} ({html.escape(DISPLAY_TIMEZONE)})</small></p></main></body></html>\n"""


def build_logo() -> str:
    return """<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 512 512"><rect width="512" height="512" rx="112" fill="#171a21"/><rect x="88" y="126" width="336" height="238" rx="42" fill="none" stroke="#f5f7fa" stroke-width="28"/><path d="M205 92l51 42 51-42" fill="none" stroke="#f5f7fa" stroke-width="24" stroke-linecap="round"/><circle cx="368" cy="314" r="28" fill="#1ed760"/></svg>\n"""


def main() -> None:
    if not PLAYLIST.exists():
        raise SystemExit(f"Missing {PLAYLIST}. Run cleaner.py first.")

    channels = parse_playlist(PLAYLIST)
    if not channels:
        raise SystemExit("No channels were parsed from playlist.m3u")

    records, feeds = load_database()
    hits = enrich(channels, records, feeds)
    print(f"  matched {hits}/{len(channels)} channels to a database record")

    epg, generated = load_epg()
    zone = local_zone()

    channels.sort(key=lambda c: (c.display_name or c.name).casefold())
    popular = pick_popular(channels)
    popular_ids = {c.id for c in popular}

    if SITE_DIR.exists():
        shutil.rmtree(SITE_DIR)
    SITE_DIR.mkdir(parents=True)

    write_json(SITE_DIR / "manifest.json", build_manifest())

    previews = [meta_preview(channel) for channel in channels]
    write_json(SITE_DIR / "catalog/tv/live-tv.json", {"metas": previews})

    popular_previews = [meta_preview(c) for c in popular]
    write_json(SITE_DIR / "catalog/tv/popular.json", {"metas": popular_previews})

    for genre in GENRE_FILTERS:
        if genre == "Popular":
            filtered = popular_previews
        else:
            filtered = [
                meta_preview(c)
                for c in channels
                if genre in genre_memberships(c, popular_ids)
            ]
        write_json(
            SITE_DIR / "catalog/tv/live-tv" / f"genre={genre}.json",
            {"metas": filtered},
        )

    guide_count = 0
    for channel in channels:
        write_json(
            SITE_DIR / "meta/tv" / f"{channel.id}.json",
            {"meta": full_meta(channel, popular_ids, epg, generated, zone)},
        )
        write_json(
            SITE_DIR / "stream/tv" / f"{channel.id}.json",
            stream_response(channel, epg, generated, zone),
        )
        if epg.get(channel.tvg_id or ""):
            guide_count += 1

    stream_count = sum(len(channel.streams) for channel in channels)
    (SITE_DIR / "index.html").write_text(
        build_index(len(channels), stream_count, popular, guide_count, generated, zone),
        encoding="utf-8",
    )
    (SITE_DIR / "addon-logo.svg").write_text(build_logo(), encoding="utf-8")
    (SITE_DIR / ".nojekyll").write_text("", encoding="utf-8")

    print(f"Built static Stremio addon: {len(channels)} channels / {stream_count} streams")
    print(f"Channels with programme data: {guide_count}")
    print("Popular row:")
    for rank, channel in enumerate(popular, start=1):
        print(f"  {rank:2}. {channel.display_name}")
    print("Categories:")
    for genre in GENRE_FILTERS:
        count = sum(genre in genre_memberships(c, popular_ids) for c in channels)
        print(f"  {genre}: {count}")


if __name__ == "__main__":
    main()
