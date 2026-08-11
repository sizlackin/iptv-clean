# iptv-clean

Automatic Canada + USA IPTV cleaner and custom Stremio live-TV addon.

## Stremio addon

**Addon page:** https://sizlackin.github.io/iptv-clean/

**Manifest URL:** https://sizlackin.github.io/iptv-clean/manifest.json

The GitHub Actions workflow rebuilds the cleaned IPTV playlist and the static Stremio addon automatically. The addon groups the live channels into useful filters including Canada, USA, Sports, News, Movies, Kids, and Entertainment.

The Stremio site is generated from `playlist.m3u` by `build_stremio_addon.py` and deployed with GitHub Pages. Channels sharing the same `tvg-id` are combined into one card while retaining alternate stream URLs.

## First-time GitHub Pages setup

If the addon page is not live yet, open **Settings → Pages** for this repository and set **Source** to **GitHub Actions**. Then open **Actions → Update IPTV playlist + Stremio addon → Run workflow**.
