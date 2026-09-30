# iptv-clean

Automatic Canada + USA IPTV cleaner and custom Stremio live-TV addon.

## Stremio addon

**Addon page:** https://sizlackin.github.io/iptv-clean/

**Manifest URL:** https://sizlackin.github.io/iptv-clean/manifest.json

The GitHub Actions workflow rebuilds the cleaned IPTV playlist and the static Stremio addon automatically. The addon groups the live channels into useful filters including Canada, USA, Sports, News, Movies, Kids, and Entertainment.

The Stremio site is generated from `playlist.m3u` by `build_stremio_addon.py` and deployed with GitHub Pages. Channels sharing the same `tvg-id` are combined into one card while retaining alternate stream URLs.

## First-time GitHub Pages setup

If the addon page is not live yet, open **Settings → Pages** for this repository and set **Source** to **GitHub Actions**. Then open **Actions → Update IPTV playlist + Stremio addon → Run workflow**.

## What runs every day

1. `cleaner.py` builds `playlist.m3u` from iptv-org (Canada + USA, plus hand-picked extras like Mega from Chile) and adds every backup link iptv-org knows for those channels.
2. `check_streams.py` tries every link and records the result in `stream-status.json`. A link that fails on 2 different days in a row is left out of the addon; a channel with no working links is hidden until one comes back. Nothing is deleted from `playlist.m3u`.
3. `epg.py` builds the programme guide. It finds the current guide files on epgshare01 by name, so renamed files don't break it.
4. `build_stremio_addon.py` builds the Stremio addon, including search and a `status.json` health report at https://sizlackin.github.io/iptv-clean/status.json
