#!/usr/bin/env python3
"""Build a static Stremio live-TV addon from the cleaned playlist.m3u.

The output in site/ is suitable for GitHub Pages. Channels with the same tvg-id
are collapsed into one Stremio card while retaining all unique stream URLs.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

PLAYLIST = Path("playlist.m3u")
SITE_DIR = Path("site")
SITE_BASE = "https://sizlackin.github.io/iptv-clean"
ADDON_ID_PREFIX = "iptv_"
ADDON_VERSION = "1.0.0"

GENRE_FILTERS = [
    "Canada",
    "USA",
    "Sports",
    "News",
    "Movies",
    "Kids",
    "Entertainment",
]

ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')


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


def genre_memberships(channel: Channel) -> set[str]:
    result: set[str] = set()
    if channel.country in ("Canada", "USA"):
        result.add(channel.country)

    group = (channel.group or "").casefold()
    name = channel.name.casefold()
    text = f"{group} {name}"
    if "sport" in text:
        result.add("Sports")
    if "news" in text:
        result.add("News")
    if "movie" in text or "cinema" in text:
        result.add("Movies")
    if "kids" in text or "children" in text or "childrens" in text:
        result.add("Kids")
    if "entertainment" in text or group == "general":
        result.add("Entertainment")
    return result


def meta_preview(channel: Channel) -> dict[str, object]:
    meta: dict[str, object] = {
        "id": channel.id,
        "type": "tv",
        "name": channel.name,
        "posterShape": "poster",
    }
    if channel.poster:
        meta["poster"] = channel.poster
    return meta


def full_meta(channel: Channel) -> dict[str, object]:
    parts = ["Live TV"]
    if channel.country != "Unknown":
        parts.append(channel.country)
    if channel.group:
        parts.append(channel.group)

    meta = meta_preview(channel)
    meta.update(
        {
            "description": " • ".join(parts) + "\nPublic stream indexed by iptv-org.",
            "releaseInfo": "LIVE",
            "genres": sorted(genre_memberships(channel)),
        }
    )
    return meta


def stream_response(channel: Channel) -> dict[str, object]:
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

        item: dict[str, object] = {
            "url": entry.url,
            "name": "Live TV" if len(channel.streams) == 1 else f"Live TV • Source {index}",
            "description": f"{channel.name}\n{host}",
            "behaviorHints": behavior,
        }
        streams.append(item)
    return {"streams": streams}


def build_manifest() -> dict[str, object]:
    return {
        "id": "com.sizlackin.livetv",
        "version": ADDON_VERSION,
        "name": "Cesar Live TV",
        "description": "Clean Canada + USA live TV generated automatically from the public iptv-org playlists.",
        "logo": f"{SITE_BASE}/addon-logo.svg",
        "resources": [
            "catalog",
            {"name": "meta", "types": ["tv"], "idPrefixes": [ADDON_ID_PREFIX]},
            {"name": "stream", "types": ["tv"], "idPrefixes": [ADDON_ID_PREFIX]},
        ],
        "types": ["tv"],
        "idPrefixes": [ADDON_ID_PREFIX],
        "catalogs": [
            {
                "type": "tv",
                "id": "live-tv",
                "name": "Live TV",
                "genres": GENRE_FILTERS,
                "extra": [{"name": "genre", "options": GENRE_FILTERS, "isRequired": False}],
            }
        ],
    }


def build_index(channel_count: int, stream_count: int) -> str:
    manifest_url = f"{SITE_BASE}/manifest.json"
    stremio_url = manifest_url.replace("https://", "stremio://", 1)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cesar Live TV</title>
<style>
:root{{color-scheme:dark;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}body{{margin:0;background:#0f1115;color:#f4f5f7;display:grid;min-height:100vh;place-items:center}}main{{width:min(680px,calc(100% - 36px));background:#171a21;border:1px solid #2a2f3a;border-radius:24px;padding:28px;box-sizing:border-box}}.dot{{display:inline-block;width:10px;height:10px;border-radius:50%;background:#1ed760;box-shadow:0 0 14px #1ed76088;margin-right:8px}}h1{{margin:.25rem 0 1rem;font-size:2rem}}p{{color:#b8bec9;line-height:1.5}}.stats{{display:flex;gap:12px;flex-wrap:wrap;margin:20px 0}}.stat{{background:#101217;border:1px solid #292e38;border-radius:14px;padding:12px 16px}}.stat b{{display:block;font-size:1.25rem;color:white}}a.button{{display:block;text-align:center;background:#1ed760;color:#06130a;text-decoration:none;font-weight:800;padding:15px 18px;border-radius:999px;margin-top:18px}}code{{display:block;background:#0d0f13;border-radius:12px;padding:12px;overflow-wrap:anywhere;color:#dfe4ec}}</style></head>
<body><main><div><span class="dot"></span>GitHub-powered</div><h1>Cesar Live TV</h1><p>Your custom Stremio live-TV addon. It is rebuilt automatically from your cleaned Canada + USA IPTV playlist.</p><div class="stats"><div class="stat"><b>{channel_count}</b>channels</div><div class="stat"><b>{stream_count}</b>streams</div></div><a class="button" href="{html.escape(stremio_url)}">Install in Stremio</a><p>Manual addon URL:</p><code>{html.escape(manifest_url)}</code></main></body></html>\n"""


def build_logo() -> str:
    return """<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 512 512"><rect width="512" height="512" rx="112" fill="#171a21"/><rect x="88" y="126" width="336" height="238" rx="42" fill="none" stroke="#f5f7fa" stroke-width="28"/><path d="M205 92l51 42 51-42" fill="none" stroke="#f5f7fa" stroke-width="24" stroke-linecap="round"/><circle cx="368" cy="314" r="28" fill="#1ed760"/></svg>\n"""


def main() -> None:
    if not PLAYLIST.exists():
        raise SystemExit(f"Missing {PLAYLIST}. Run cleaner.py first.")

    channels = parse_playlist(PLAYLIST)
    if not channels:
        raise SystemExit("No channels were parsed from playlist.m3u")

    if SITE_DIR.exists():
        shutil.rmtree(SITE_DIR)
    SITE_DIR.mkdir(parents=True)

    write_json(SITE_DIR / "manifest.json", build_manifest())

    previews = [meta_preview(channel) for channel in channels]
    write_json(SITE_DIR / "catalog/tv/live-tv.json", {"metas": previews})

    for genre in GENRE_FILTERS:
        filtered = [meta_preview(c) for c in channels if genre in genre_memberships(c)]
        write_json(
            SITE_DIR / "catalog/tv/live-tv" / f"genre={genre}.json",
            {"metas": filtered},
        )

    for channel in channels:
        write_json(SITE_DIR / "meta/tv" / f"{channel.id}.json", {"meta": full_meta(channel)})
        write_json(SITE_DIR / "stream/tv" / f"{channel.id}.json", stream_response(channel))

    stream_count = sum(len(channel.streams) for channel in channels)
    (SITE_DIR / "index.html").write_text(
        build_index(len(channels), stream_count), encoding="utf-8"
    )
    (SITE_DIR / "addon-logo.svg").write_text(build_logo(), encoding="utf-8")
    (SITE_DIR / ".nojekyll").write_text("", encoding="utf-8")

    print(f"Built static Stremio addon: {len(channels)} channels / {stream_count} streams")
    for genre in GENRE_FILTERS:
        print(f"  {genre}: {sum(genre in genre_memberships(c) for c in channels)}")


if __name__ == "__main__":
    main()
