#!/usr/bin/env python3
"""Build a cleaner Canada + USA M3U playlist from iptv-org.

The script keeps stream URLs, tvg-id values and other metadata intact, cleans
human-visible channel titles, and converts channel logos into portrait-friendly
poster URLs so Stremio does not crop square/wide station logos.
"""

from __future__ import annotations

import re
import urllib.parse
import urllib.request
from pathlib import Path

SOURCES = [
    ("Canada", "https://iptv-org.github.io/iptv/countries/ca.m3u"),
    ("USA", "https://iptv-org.github.io/iptv/countries/us.m3u"),
]
OUTPUT = Path("playlist.m3u")

# Stremio shows TV entries using portrait cards. Most IPTV logos are square or
# wide, so Stremio's poster crop can cut them off. wsrv.nl places each original
# logo inside a real 2:3 portrait image while preserving its aspect ratio.
POSTER_WIDTH = 600
POSTER_HEIGHT = 900
POSTER_BACKGROUND = "181818"

# Edit this dictionary whenever you want a specific channel to have an exact
# name. The key is tvg-id and the value is your preferred visible title.
OVERRIDES: dict[str, str] = {
    # Example:
    # "CBLTDT.ca": "CBC Toronto",
}

NOISE_PARENS = re.compile(
    r"\s*[\(\[]\s*(?:"
    r"\d{3,4}p(?:\d+)?|\d{3,4}i|4k|uhd|fhd|full\s*hd|hd|sd|"
    r"hevc|h\.?(?:264|265)|x(?:264|265)|"
    r"not\s*24/?7|24/?7|geo[- ]?blocked|geoblocked|"
    r"offline|backup|mirror|raw|"
    r"en|eng|english|fr|fre|fra|french|es|spa|spanish"
    r")\s*[\)\]]",
    re.IGNORECASE,
)

TRAILING_QUALITY = re.compile(
    r"\s+(?:\d{3,4}p(?:\d+)?|\d{3,4}i|4K|UHD|FHD|HD|SD)\s*$",
    re.IGNORECASE,
)

MULTISPACE = re.compile(r"\s{2,}")
EMPTY_PARENS = re.compile(r"\s*[\(\[]\s*[\)\]]")


def download(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "iptv-clean/1.1 (+GitHub Actions)"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8-sig", errors="replace")


def get_attr(extinf_prefix: str, key: str) -> str | None:
    match = re.search(rf'\b{re.escape(key)}="([^"]*)"', extinf_prefix)
    return match.group(1) if match else None


def set_attr(extinf_prefix: str, key: str, value: str) -> str:
    safe = value.replace('"', "'")
    pattern = re.compile(rf'\b{re.escape(key)}="[^"]*"')
    replacement = f'{key}="{safe}"'
    if pattern.search(extinf_prefix):
        return pattern.sub(replacement, extinf_prefix, count=1)
    return f"{extinf_prefix} {replacement}"


def clean_name(name: str, tvg_id: str | None = None) -> str:
    if tvg_id and tvg_id in OVERRIDES:
        return OVERRIDES[tvg_id]

    cleaned = name.strip()
    previous = None
    while previous != cleaned:
        previous = cleaned
        cleaned = NOISE_PARENS.sub("", cleaned)

    cleaned = TRAILING_QUALITY.sub("", cleaned)
    cleaned = EMPTY_PARENS.sub("", cleaned)
    cleaned = re.sub(r"\s+([,:;])", r"\1", cleaned)
    cleaned = re.sub(r"(?:\s*[-|•]\s*){2,}", " - ", cleaned)
    cleaned = re.sub(r"\s+[-|•]\s*$", "", cleaned)
    cleaned = MULTISPACE.sub(" ", cleaned).strip(" -|•")
    return cleaned or name.strip()


def posterize_logo(logo_url: str | None) -> str | None:
    if not logo_url:
        return None
    if "wsrv.nl/?url=" in logo_url:
        return logo_url

    encoded = urllib.parse.quote(logo_url, safe="")
    return (
        "https://wsrv.nl/?url=" + encoded
        + f"&w={POSTER_WIDTH}&h={POSTER_HEIGHT}"
        + f"&fit=contain&cbg={POSTER_BACKGROUND}&output=png"
    )


def process_playlist(text: str) -> list[str]:
    output: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")

        if line.startswith("#EXTM3U"):
            continue

        if not line.startswith("#EXTINF:"):
            output.append(line)
            continue

        if "," not in line:
            output.append(line)
            continue

        prefix, visible_name = line.rsplit(",", 1)
        tvg_id = get_attr(prefix, "tvg-id")
        new_name = clean_name(visible_name, tvg_id)

        # Use the cleaned title both as tvg-name and as the EXTINF display name.
        prefix = set_attr(prefix, "tvg-name", new_name)

        # Replace the original logo URL with a 2:3 contained poster version.
        original_logo = get_attr(prefix, "tvg-logo")
        poster_logo = posterize_logo(original_logo)
        if poster_logo:
            prefix = set_attr(prefix, "tvg-logo", poster_logo)

        output.append(f"{prefix},{new_name}")

    return output


def main() -> None:
    merged: list[str] = ["#EXTM3U"]
    seen_exact_entries: set[tuple[str, str]] = set()

    for country, url in SOURCES:
        print(f"Downloading {country}: {url}")
        text = download(url)
        processed = process_playlist(text)

        i = 0
        while i < len(processed):
            line = processed[i]
            if line.startswith("#EXTINF:"):
                block = [line]
                i += 1
                while i < len(processed) and not processed[i].startswith("#EXTINF:"):
                    block.append(processed[i])
                    i += 1
                    if block[-1] and not block[-1].startswith("#"):
                        break

                url_line = next(
                    (x for x in reversed(block) if x and not x.startswith("#")),
                    "",
                )
                key = (block[0], url_line)
                if key not in seen_exact_entries:
                    seen_exact_entries.add(key)
                    merged.extend(block)
                continue

            if line.strip():
                merged.append(line)
            i += 1

    OUTPUT.write_text("\n".join(merged).rstrip() + "\n", encoding="utf-8")
    channel_count = sum(1 for line in merged if line.startswith("#EXTINF:"))
    print(f"Wrote {channel_count} channels to {OUTPUT}")


if __name__ == "__main__":
    main()
