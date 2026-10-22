# wasemabdelhady.com

Static artist site for Wasem Abdelhady (وسيم عبد الهادي), served by GitHub Pages from `main`.
Black `#0a0a0a` + gold `#e0a458` + text `#f5efe6`; fonts Syne (EN), Reem Kufi (AR display), Cairo (AR body). No libraries.

## Working rules (from the owner)
- **Never push or merge to `main` without Wasem saying «انشر».** Work on branch `redesign`, show screenshots, then open a PR `redesign → main` and merge it.
- Before starting new work: `git fetch origin main && git checkout -B redesign origin/main`.
- Wasem works from his phone: short replies, one step at a time, Arabic (Egyptian).
- Credits must be exactly what Wasem gives — never invent names or roles.
- Hero roles: SINGER · COMPOSER · ARRANGER.

## Layout
| Path | What |
| --- | --- |
| `index.html` | Home page (hand-edited) |
| `<slug>/index.html` | Song pages — **generated** by `tools/mksongs.py` |
| `press/index.html` | Press kit — **generated** by `tools/mkpress.py` from `tools/press.tpl.html` |
| `write/index.html` | Contact form — from `tools/write.tpl.html` (Web3Forms, key in `assets/write.js`) |
| `new/index.html` | Redirect to the latest release (used as the bio link) |
| `qr/index.html` | Target of the printed QR code: counted in analytics, then forwards to `/new/` (never change the URL `/qr`) |
| `covers/` | `<slug>.jpg` (900px), `<slug>.webp`, `<slug>-400.webp` |
| `audio/` | `<slug>.mp3` — 20s preview, 128k, 0.4s fade in / 0.6s fade out |
| `assets/` | `song.css`, `song.js`, `share.js` (story images), `press.*`, `write.*` |
| `tools/` | Generators (excluded from the site by `_config.yml`) |

Every page ends with the Cloudflare Web Analytics beacon (token `b26b5bf0eb62472c987c27890a42bb25`). Keep it on new pages; `new/` has none on purpose.

## Adding a new release (checklist)
Wasem sends: title AR/EN, release date, Spotify / Apple Music / Anghami links, YouTube video, 20s MP3 (or full track + timestamp), cover (square), credits, lyrics (or "in Notion" — page under «🎵 الأغاني»).

1. **Assets**: cover → `covers/<slug>.jpg` (900×900, q82) + `.webp` (q80) + `-400.webp`; MP3 → `audio/<slug>.mp3` with `ffmpeg -af "afade=t=in:d=0.4,afade=t=out:st=19.4:d=0.6" -b:a 128k -map_metadata -1`.
2. **Colours**: pick `amb` (dominant dark tone, brightened) and `acc` (most saturated accent) from the cover.
3. **`tools/mksongs.py`**: add the song as the **first** entry of `SONGS` (fields as the others; `*` marks chorus lines, `!` marks hook lines such as the song's repeated question). Run `python3 tools/mksongs.py . tools/sprite.html`.
4. **`index.html`**:
   - «أحدث إصدار» (`#latest`): cover, play button data-attrs, title, platform links, `--amb/--acc`, link to the song page.
   - «الأغاني» (`.songs`): add a new `<article class="song">` first, same structure as the others (credits `<details>` included).
   - «الفيديوهات» (`.videos`): new `video--main` first with `data-yt`, previous main becomes a normal card.
   - Lyric pull-quote (`.lyric`): one strong line from the new song (ask Wasem or pick the Notion «مرشّح رقم ١»).
   - JSON-LD `track` list and the `#f-song` options in `tools/write.tpl.html`.
5. **`new/index.html`**: point all three places to `/<slug>/`.
6. **Press kit**: releases list in `tools/press.tpl.html`, «أحدث إصدار» fact, and the bios in `tools/mkpress.py` if relevant; run `python3 tools/mkpress.py . tools/sprite.html`.
7. **`sitemap.xml`**: add `/<slug>/`.
8. Screenshot phone + desktop, send to Wasem, wait for «انشر», then PR + merge.

## Coming-soon mode
In `index.html`, set `NEXT_RELEASE = { ar, en, date: 'YYYY-MM-DDTHH:MM:SS+02:00', presave: '<DistroKid HyperFollow>', cover: 'covers/<slug>.webp' }`. The section appears with a countdown and hides itself at release time. Set back to `null` when the release is added.

## Other regenerated assets
- Share image: `node tools/og.mjs` (needs Playwright; set `CHROME` to a Chromium path if needed) writes `og.png` → convert to `og.jpg` (q86).
- Icons: WA monogram (Cormorant Garamond 600, gold ring) at 32/180/192/512.
