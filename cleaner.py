#!/usr/bin/env python3
"""Build a clean Canada + USA IPTV playlist for Stremio.

- Merges iptv-org Canada + USA playlists.
- Cleans channel titles while preserving stream metadata.
- Wraps real logos in 600x900 portrait images via wsrv.nl.
- Generates deterministic fallback posters when a logo is missing or confirmed dead.
- Conservatively probes logo health and requires two definitive failures before fallback.
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
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
except ImportError:
    HAVE_PIL = False

SOURCES = [
    ("Canada", "https://iptv-org.github.io/iptv/countries/ca.m3u"),
    ("USA", "https://iptv-org.github.io/iptv/countries/us.m3u"),
]

OUTPUT = Path("playlist.m3u")
POSTERS_DIR = Path("posters")
LOGO_STATUS_FILE = Path("logo-status.json")
REPO_RAW_BASE = "https://raw.githubusercontent.com/sizlackin/iptv-clean/main"

POSTER_WIDTH = 600
POSTER_HEIGHT = 900
POSTER_BACKGROUND = "181818"
POSTER_BACKGROUND_RGB = (0x18, 0x18, 0x18)
POSTER_TEXT_RGB = (0xEE, 0xEE, 0xEE)
POSTER_MARGIN = 56
POSTER_FONT_MAX = 76
POSTER_FONT_MIN = 24
POSTER_MAX_LINES = 5

# iptv-org already embeds every logo its own database can supply in these
# playlists. Keep the optional lookup disabled to avoid a ~7 MB download/run.
ENABLE_IPTV_ORG_LOGO_REPAIR = False
LOGOS_API_URL = "https://iptv-org.github.io/api/logos.json"

# Broken-logo detection is intentionally conservative. Network failures,
# timeouts, 403/429 and 5xx are UNKNOWN and never remove a real logo.
ENABLE_LOGO_HEALTH_CHECK = True
LOGO_CHECK_WORKERS = 12
LOGO_CHECK_TIMEOUT = 12
LOGO_RECHECK_DAYS = 7
LOGO_FAILURES_BEFORE_BROKEN = 2

OVERRIDES: dict[str, str] = {
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
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "iptv-clean/1.3 (+GitHub Actions)"}
            )
            with urllib.request.urlopen(req, timeout=60) as response:
                return response.read().decode("utf-8-sig", errors="replace")
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            print(f"  Attempt {attempt}/{retries} failed for {url}: {exc}")
            if attempt < retries:
                time.sleep(backoff * attempt)
    assert last_error is not None
    raise last_error


def get_attr(prefix: str, key: str) -> str | None:
    match = re.search(rf'\b{re.escape(key)}="([^"]*)"', prefix)
    return match.group(1) if match else None


def set_attr(prefix: str, key: str, value: str) -> str:
    safe = value.replace('"', "'")
    pattern = re.compile(rf'\b{re.escape(key)}="[^"]*"')
    replacement = f'{key}="{safe}"'
    if pattern.search(prefix):
        return pattern.sub(replacement, prefix, count=1)
    return f"{prefix} {replacement}"


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


def posterize_logo(url: str | None) -> str | None:
    if not url:
        return None
    if "wsrv.nl/?url=" in url:
        return url
    encoded = urllib.parse.quote(url, safe="")
    return (
        f"https://wsrv.nl/?url={encoded}"
        f"&w={POSTER_WIDTH}&h={POSTER_HEIGHT}"
        f"&fit=contain&cbg={POSTER_BACKGROUND}&output=png"
    )


def load_logo_lookup() -> dict[str, str]:
    """Optional iptv-org logo index keyed by exact feed and bare tvg-id."""
    if not ENABLE_IPTV_ORG_LOGO_REPAIR:
        return {}
    print(f"Downloading logo index: {LOGOS_API_URL}")
    try:
        entries = json.loads(download(LOGOS_API_URL))
    except Exception as exc:
        print(f"  Logo index unavailable: {exc}")
        return {}

    bare_any: dict[str, str] = {}
    bare_in_use: dict[str, str] = {}
    bare_main: dict[str, str] = {}
    by_feed: dict[str, str] = {}
    for entry in entries:
        channel, url = entry.get("channel"), entry.get("url")
        if not channel or not url:
            continue
        feed = entry.get("feed")
        if feed:
            by_feed.setdefault(f"{channel}@{feed}", url)
        bare_any.setdefault(channel, url)
        if entry.get("in_use", True):
            bare_in_use.setdefault(channel, url)
            if feed is None:
                bare_main.setdefault(channel, url)

    lookup = dict(bare_any)
    lookup.update(bare_in_use)
    lookup.update(bare_main)
    lookup.update(by_feed)
    print(f"  Loaded logos for {len(bare_any)} channels")
    return lookup


def _load_font(size: int):
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ):
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _wrap_to_width(text: str, font, draw, max_width: int) -> list[str] | None:
    def width(value: str) -> int:
        box = draw.textbbox((0, 0), value, font=font)
        return box[2] - box[0]

    lines: list[str] = []
    current = ""
    for word in text.split():
        if width(word) > max_width:
            return None
        candidate = f"{current} {word}".strip()
        if current and width(candidate) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def generate_fallback_poster(channel_name: str, tvg_id: str | None) -> Path | None:
    if not HAVE_PIL:
        return None

    slug = hashlib.sha1((tvg_id or channel_name).encode("utf-8")).hexdigest()[:12]
    out_path = POSTERS_DIR / f"{slug}.png"
    image = Image.new("RGB", (POSTER_WIDTH, POSTER_HEIGHT), POSTER_BACKGROUND_RGB)
    draw = ImageDraw.Draw(image)
    text = channel_name.upper().strip() or "CHANNEL"
    max_width = POSTER_WIDTH - 2 * POSTER_MARGIN
    max_height = POSTER_HEIGHT - 2 * POSTER_MARGIN

    chosen_font = _load_font(POSTER_FONT_MIN)
    chosen_lines = [text]
    chosen_gap = 16
    for size in range(POSTER_FONT_MAX, POSTER_FONT_MIN - 1, -4):
        font = _load_font(size)
        lines = _wrap_to_width(text, font, draw, max_width)
        if not lines or len(lines) > POSTER_MAX_LINES:
            continue
        gap = max(8, size // 4)
        heights = [
            draw.textbbox((0, 0), line, font=font)[3]
            - draw.textbbox((0, 0), line, font=font)[1]
            for line in lines
        ]
        if sum(heights) + gap * (len(lines) - 1) <= max_height:
            chosen_font, chosen_lines, chosen_gap = font, lines, gap
            break

    boxes = [draw.textbbox((0, 0), line, font=chosen_font) for line in chosen_lines]
    heights = [box[3] - box[1] for box in boxes]
    total_height = sum(heights) + chosen_gap * (len(chosen_lines) - 1)
    y = (POSTER_HEIGHT - total_height) / 2
    for line, box, height in zip(chosen_lines, boxes, heights):
        x = (POSTER_WIDTH - (box[2] - box[0])) / 2 - box[0]
        draw.text((x, y - box[1]), line, font=chosen_font, fill=POSTER_TEXT_RGB)
        y += height + chosen_gap

    POSTERS_DIR.mkdir(exist_ok=True)
    image.save(out_path, format="PNG")
    return out_path


def probe_logo(url: str) -> str:
    """Return ok, broken, or unknown. 'broken' requires definitive evidence."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "iptv-clean/1.3 (+GitHub Actions)",
            "Range": "bytes=0-2047",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=LOGO_CHECK_TIMEOUT) as response:
            final_url = response.geturl().lower()
            content_type = (response.headers.get("Content-Type") or "").lower()
            body = response.read(2048)
            if "removed.png" in final_url:
                return "broken"
            if content_type and not content_type.startswith("image/"):
                return "broken"
            if len(body) < 100 and not response.headers.get("Content-Range"):
                return "broken"
            return "ok"
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 410):
            return "broken"
        return "unknown"
    except Exception:
        return "unknown"


def find_broken_logos(logo_urls: set[str]) -> set[str]:
    if not ENABLE_LOGO_HEALTH_CHECK:
        return set()

    try:
        status = json.loads(LOGO_STATUS_FILE.read_text(encoding="utf-8"))
        if not isinstance(status, dict):
            status = {}
    except (OSError, json.JSONDecodeError):
        status = {}

    today_date = dt.date.today()
    today = today_date.isoformat()

    def stale(record: dict) -> bool:
        if record.get("failures", 0) > 0:
            return True
        checked = record.get("checked")
        if not checked:
            return True
        try:
            return (today_date - dt.date.fromisoformat(checked)).days >= LOGO_RECHECK_DAYS
        except (TypeError, ValueError):
            return True

    to_check = [url for url in sorted(logo_urls) if stale(status.get(url, {}))]
    print(f"Checking {len(to_check)} logo URLs ({len(logo_urls)} total, rest cached)")

    if to_check:
        with concurrent.futures.ThreadPoolExecutor(max_workers=LOGO_CHECK_WORKERS) as pool:
            for url, verdict in zip(to_check, pool.map(probe_logo, to_check)):
                record = status.setdefault(url, {})
                if verdict == "ok":
                    record["failures"] = 0
                    record["checked"] = today
                elif verdict == "broken":
                    record["failures"] = int(record.get("failures", 0)) + 1
                    record["checked"] = today

    status = {url: record for url, record in status.items() if url in logo_urls}
    LOGO_STATUS_FILE.write_text(
        json.dumps(status, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )

    broken = {
        url
        for url, record in status.items()
        if int(record.get("failures", 0)) >= LOGO_FAILURES_BEFORE_BROKEN
    }
    pending = sum(
        1
        for record in status.values()
        if 0 < int(record.get("failures", 0)) < LOGO_FAILURES_BEFORE_BROKEN
    )
    print(f"  Confirmed broken: {len(broken)} (plus {pending} failing once)")
    return broken


def collect_logo_urls(texts: list[str], logo_lookup: dict[str, str]) -> set[str]:
    urls = set(logo_lookup.values())
    for text in texts:
        for line in text.splitlines():
            if line.startswith("#EXTINF:"):
                logo = get_attr(line, "tvg-logo")
                if logo:
                    urls.add(logo)
    return urls


def resolve_logo(
    prefix: str,
    channel_name: str,
    tvg_id: str | None,
    logo_lookup: dict[str, str],
    broken_logos: set[str],
) -> str | None:
    original = get_attr(prefix, "tvg-logo")
    if original and original not in broken_logos:
        return posterize_logo(original)

    if tvg_id:
        for key in (tvg_id, tvg_id.split("@", 1)[0]):
            candidate = logo_lookup.get(key)
            if candidate and candidate not in broken_logos:
                return posterize_logo(candidate)

    generated = generate_fallback_poster(channel_name, tvg_id)
    if generated:
        return f"{REPO_RAW_BASE}/{generated.as_posix()}"
    return None


def process_playlist(
    text: str,
    logo_lookup: dict[str, str],
    broken_logos: set[str],
) -> list[str]:
    output: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if line.startswith("#EXTM3U"):
            continue
        if not line.startswith("#EXTINF:") or "," not in line:
            output.append(line)
            continue

        prefix, visible_name = line.rsplit(",", 1)
        tvg_id = get_attr(prefix, "tvg-id")
        new_name = clean_name(visible_name, tvg_id)
        prefix = set_attr(prefix, "tvg-name", new_name)

        logo = resolve_logo(prefix, new_name, tvg_id, logo_lookup, broken_logos)
        if logo:
            prefix = set_attr(prefix, "tvg-logo", logo)
        output.append(f"{prefix},{new_name}")
    return output


def main() -> None:
    logo_lookup = load_logo_lookup()

    texts: list[tuple[str, str]] = []
    for country, url in SOURCES:
        print(f"Downloading {country}: {url}")
        texts.append((country, download(url)))

    all_text = [text for _, text in texts]
    broken_logos = find_broken_logos(collect_logo_urls(all_text, logo_lookup))

    merged: list[str] = ["#EXTM3U"]
    seen_exact_entries: set[tuple[str, str]] = set()
    for _, text in texts:
        processed = process_playlist(text, logo_lookup, broken_logos)
        i = 0
        while i < len(processed):
            line = processed[i]
            if not line.startswith("#EXTINF:"):
                if line.strip():
                    merged.append(line)
                i += 1
                continue

            block = [line]
            i += 1
            while i < len(processed) and not processed[i].startswith("#EXTINF:"):
                block.append(processed[i])
                i += 1
            stream_url = next(
                (item for item in reversed(block) if item and not item.startswith("#")),
                "",
            )
            key = (block[0], stream_url)
            if key not in seen_exact_entries:
                seen_exact_entries.add(key)
                merged.extend(block)

    OUTPUT.write_text("\n".join(merged).rstrip() + "\n", encoding="utf-8")
    count = sum(1 for line in merged if line.startswith("#EXTINF:"))
    print(f"Wrote {count} channels to {OUTPUT}")


if __name__ == "__main__":
    main()
