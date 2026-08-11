#!/usr/bin/env python3
"""Build a cleaner Canada + USA M3U playlist from iptv-org.

The script keeps stream URLs, tvg-id values and other metadata intact, cleans
human-visible channel titles, and converts channel logos into portrait-friendly
poster URLs so Stremio does not crop square/wide station logos.

When a channel has no usable logo, it looks up an official logo from the
iptv-org API (keyed by tvg-id) and, failing that, generates a deterministic
dark 600x900 fallback poster with the channel name on it. Fallback posters
are written to posters/ and referenced via raw.githubusercontent.com so the
whole thing stays static/GitHub-hosted.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
    HAVE_PIL = True
except ImportError:  # Pillow missing -> skip fallback poster generation gracefully
    HAVE_PIL = False

SOURCES = [
    ("Canada", "https://iptv-org.github.io/iptv/countries/ca.m3u"),
    ("USA", "https://iptv-org.github.io/iptv/countries/us.m3u"),
]

# iptv-org's own logo index, keyed by channel (tvg-id).
#
# NOTE: measured against the live CA+US playlists, this repairs ZERO logos.
# iptv-org already embeds tvg-logo in the playlist wherever its database has
# one, so the only channels with a blank tvg-logo are ones it has no logo for
# at all. Leaving this off avoids a ~7 MB download on every run. Flip it to
# True if you ever want the extra safety net (e.g. if upstream changes how
# playlists are generated) — the code path is tested and works.
ENABLE_IPTV_ORG_LOGO_REPAIR = False
LOGOS_API_URL = "https://iptv-org.github.io/api/logos.json"

OUTPUT = Path("playlist.m3u")
POSTERS_DIR = Path("posters")

# Raw GitHub base used to reference generated fallback posters. Update the
# owner/repo if this script is ever forked/renamed.
REPO_RAW_BASE = "https://raw.githubusercontent.com/sizlackin/iptv-clean/main"

# Stremio shows TV entries using portrait cards. Most IPTV logos are square or
# wide, so Stremio's poster crop can cut them off. wsrv.nl places each original
# logo inside a real 2:3 portrait image while preserving its aspect ratio.
POSTER_WIDTH = 600
POSTER_HEIGHT = 900
POSTER_BACKGROUND = "181818"
POSTER_BACKGROUND_RGB = (0x18, 0x18, 0x18)
POSTER_TEXT_RGB = (0xEE, 0xEE, 0xEE)
POSTER_MARGIN = 56
POSTER_FONT_MAX = 76
POSTER_FONT_MIN = 24
POSTER_MAX_LINES = 5

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


def download(url: str, retries: int = 3, backoff: float = 2.0) -> str:
    """Download a URL as text, retrying transient failures.

    A single flaky fetch (common with GitHub Pages / third-party APIs)
    should not blow up the whole run and leave the repo without an update.
    """
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "iptv-clean/1.2 (+GitHub Actions)"},
            )
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read().decode("utf-8-sig", errors="replace")
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            print(f"  Attempt {attempt}/{retries} failed for {url}: {exc}")
            if attempt < retries:
                time.sleep(backoff * attempt)
    assert last_error is not None
    raise last_error


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


# ---------------------------------------------------------------------------
# Missing-logo repair: look up an official logo from the iptv-org API.
# ---------------------------------------------------------------------------

def load_logo_lookup() -> dict[str, str]:
    """Build a logo lookup from iptv-org's logos.json.

    Returns a dict keyed by both "Channel.cc@FEED" and bare "Channel.cc",
    because playlist tvg-id values carry a feed suffix (e.g. "CanadaOne.ca@SD")
    while logos.json stores the bare channel ID plus a separate `feed` field.
    Looking up only the bare ID would silently match nothing.
    """
    print(f"Downloading logo index: {LOGOS_API_URL}")
    try:
        raw = download(LOGOS_API_URL)
    except Exception as exc:  # noqa: BLE001 - never let this abort the run
        print(f"  Could not fetch logo index, skipping logo repair: {exc}")
        return {}

    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"  Could not parse logo index, skipping logo repair: {exc}")
        return {}

    # Three priority tiers for the bare-channel key, best first: (a) main feed
    # + currently in use, (b) any feed but still in use, (c) anything at all
    # (last resort — a delisted logo still beats no logo).
    best: dict[str, str] = {}
    in_use_any: dict[str, str] = {}
    any_entry: dict[str, str] = {}
    # Exact feed matches, keyed "Channel.cc@FEED".
    by_feed: dict[str, str] = {}

    for entry in entries:
        channel = entry.get("channel")
        url = entry.get("url")
        if not channel or not url:
            continue

        feed = entry.get("feed")
        if feed:
            by_feed.setdefault(f"{channel}@{feed}", url)

        any_entry.setdefault(channel, url)
        if entry.get("in_use", True):
            in_use_any.setdefault(channel, url)
            if feed is None:
                best.setdefault(channel, url)

    lookup: dict[str, str] = dict(any_entry)
    lookup.update(in_use_any)
    lookup.update(best)
    # Feed-specific keys live in the same dict under a distinct key shape.
    lookup.update(by_feed)

    print(f"  Loaded logos for {len(any_entry)} channels from iptv-org")
    return lookup


# ---------------------------------------------------------------------------
# Fallback poster generation for channels with no logo anywhere.
# ---------------------------------------------------------------------------

def _load_font(size: int) -> "ImageFont.FreeTypeFont | ImageFont.ImageFont":
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _wrap_to_width(
    text: str,
    font: "ImageFont.FreeTypeFont | ImageFont.ImageFont",
    draw: "ImageDraw.ImageDraw",
    max_width: int,
) -> list[str] | None:
    """Wrap text so every line fits within max_width, measured in pixels.

    Returns None if any single word is itself too wide to fit, which tells
    the caller to retry with a smaller font.
    """
    def width_of(s: str) -> int:
        bbox = draw.textbbox((0, 0), s, font=font)
        return bbox[2] - bbox[0]

    lines: list[str] = []
    current = ""
    for word in text.split():
        if width_of(word) > max_width:
            return None
        candidate = f"{current} {word}".strip()
        if width_of(candidate) > max_width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def generate_fallback_poster(channel_name: str, tvg_id: str | None) -> Path | None:
    """Create a deterministic dark 600x900 poster with the channel name.

    Filename is derived from tvg-id (or the name if no tvg-id) so repeated
    runs overwrite the same file instead of accumulating duplicates.

    Font size is chosen by measuring the rendered text, stepping down until
    the whole name fits inside the margins — character-count wrapping alone
    lets wide names like "LOVEWORLD USA" spill off the edge.
    """
    if not HAVE_PIL:
        return None

    key = tvg_id or channel_name
    slug = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    out_path = POSTERS_DIR / f"{slug}.png"

    image = Image.new("RGB", (POSTER_WIDTH, POSTER_HEIGHT), POSTER_BACKGROUND_RGB)
    draw = ImageDraw.Draw(image)

    text = channel_name.upper().strip() or "CHANNEL"
    max_width = POSTER_WIDTH - (2 * POSTER_MARGIN)
    max_height = POSTER_HEIGHT - (2 * POSTER_MARGIN)

    chosen_lines: list[str] = [text]
    chosen_font = _load_font(POSTER_FONT_MIN)
    line_gap = 16

    for size in range(POSTER_FONT_MAX, POSTER_FONT_MIN - 1, -4):
        font = _load_font(size)
        lines = _wrap_to_width(text, font, draw, max_width)
        if not lines or len(lines) > POSTER_MAX_LINES:
            continue
        line_gap = max(8, size // 4)
        total = sum(
            draw.textbbox((0, 0), ln, font=font)[3]
            - draw.textbbox((0, 0), ln, font=font)[1]
            + line_gap
            for ln in lines
        )
        if total <= max_height:
            chosen_lines, chosen_font = lines, font
            break

    heights = []
    for line in chosen_lines:
        bbox = draw.textbbox((0, 0), line, font=chosen_font)
        heights.append(bbox[3] - bbox[1])
    total_height = sum(heights) + line_gap * (len(chosen_lines) - 1)

    y = (POSTER_HEIGHT - total_height) / 2
    for line, height in zip(chosen_lines, heights):
        bbox = draw.textbbox((0, 0), line, font=chosen_font)
        x = (POSTER_WIDTH - (bbox[2] - bbox[0])) / 2 - bbox[0]
        draw.text((x, y - bbox[1]), line, font=chosen_font, fill=POSTER_TEXT_RGB)
        y += height + line_gap

    POSTERS_DIR.mkdir(exist_ok=True)
    image.save(out_path, format="PNG")
    return out_path


def resolve_logo(
    prefix: str,
    channel_name: str,
    tvg_id: str | None,
    logo_lookup: dict[str, str],
) -> str | None:
    """Return the best poster URL for a channel, in priority order:

    1. The playlist's own tvg-logo (posterized).
    2. An official iptv-org logo looked up by tvg-id (posterized).
    3. A generated fallback poster hosted in this repo.
    """
    original_logo = get_attr(prefix, "tvg-logo")
    if original_logo:
        return posterize_logo(original_logo)

    if tvg_id:
        # Try the exact feed first ("CanadaOne.ca@SD"), then the bare
        # channel ID ("CanadaOne.ca"), which is how logos.json is keyed.
        for key in (tvg_id, tvg_id.split("@", 1)[0]):
            if key in logo_lookup:
                return posterize_logo(logo_lookup[key])

    generated = generate_fallback_poster(channel_name, tvg_id)
    if generated:
        return f"{REPO_RAW_BASE}/{generated.as_posix()}"

    return None


def process_playlist(text: str, logo_lookup: dict[str, str]) -> list[str]:
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

        poster_logo = resolve_logo(prefix, new_name, tvg_id, logo_lookup)
        if poster_logo:
            prefix = set_attr(prefix, "tvg-logo", poster_logo)

        output.append(f"{prefix},{new_name}")

    return output


def main() -> None:
    logo_lookup = load_logo_lookup() if ENABLE_IPTV_ORG_LOGO_REPAIR else {}

    merged: list[str] = ["#EXTM3U"]
    seen_exact_entries: set[tuple[str, str]] = set()

    for country, url in SOURCES:
        print(f"Downloading {country}: {url}")
        text = download(url)
        processed = process_playlist(text, logo_lookup)

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
                    pass
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
