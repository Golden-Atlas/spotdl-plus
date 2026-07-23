# Third-party notices

spotdl+ stands on other people's work. Some of it we import, some of it ships
alongside the app, and all of it comes with terms. This is the honest
accounting: what we use, under what license, and where to get its source.

Short version, everything here is compatible with spotdl+'s own
GPL-3.0-or-later, which is exactly why spotdl+ is GPL instead of something more
permissive. More on that below.

## Bundled binaries (shipped inside the Windows installer)

These are separate programs. spotdl+ runs them as subprocesses and does not link
against them. They get redistributed unmodified.

| Binary | Project | License | Source |
|---|---|---|---|
| `ffmpeg.exe`, `ffprobe.exe` | FFmpeg (gyan.dev "essentials" build) | **GPL-3.0-or-later** | <https://www.gyan.dev/ffmpeg/builds/> · <https://ffmpeg.org/download.html> |
| `deno.exe` | Deno | MIT | <https://github.com/denoland/deno> |
| `fpcalc.exe` | Chromaprint | LGPL-2.1-or-later (links FFmpeg libs) | <https://github.com/acoustid/chromaprint> |

**On the FFmpeg build specifically.** That build is GPL-licensed, so anybody we
hand the installer to is entitled to its corresponding source. It's unmodified
and published by gyan.dev, the source for that exact version is at the links
above, and I'll point anybody who asks at the specific tag. If you package a
spotdl+ release yourself you inherit that obligation, so keep this file in the
package. The installer already does.

## Python dependencies (imported at runtime)

| Package | License | Why it's here |
|---|---|---|
| [mutagen](https://github.com/quodlibet/mutagen) | **GPL-2.0-or-later** | reads and writes every tag dialect (Vorbis, ID3, MP4) |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Unlicense (public domain) | the audio-acquisition engine |
| [httpx](https://github.com/encode/httpx) | BSD-3-Clause | every HTTP call the app makes itself |
| [truststore](https://github.com/sethmlarson/truststore) | MIT | uses the OS certificate store, so corporate and antivirus TLS works |
| [rapidfuzz](https://github.com/rapidfuzz/RapidFuzz) | MIT | the string similarity behind matching |
| [rich](https://github.com/Textualize/rich) | MIT | the live display |
| [typer](https://github.com/fastapi/typer) | MIT | the command surface |

## Why spotdl+ is GPL

It's arithmetic, not a stylistic choice. `mutagen` is GPL-2.0-**or-later** and
spotdl+ *imports* it, since tags aren't something you can shell out for, and the
bundled FFmpeg is GPL-3.0. Link a GPL library into your program and ship the
result and the result is GPL too. Picking MIT here would have been a license
violation wearing a friendly hat. GPL-3.0-or-later satisfies both, so that's
what spotdl+ is.

If that ever has to change, the honest path is dropping mutagen for a
permissively licensed tagger and unbundling FFmpeg. That's a real project, not a
paperwork edit.

## Services

spotdl+ talks to services with their own terms, and none of those are licenses
in the software sense.

- **Spotify Web API.** Metadata only, using *your* free developer credentials.
  spotdl+ never ships credentials and never downloads audio from Spotify.
- **MusicBrainz.** Release classification, rate-limited to their published
  guidance. We pace under 1 request per second on purpose.
- **AcoustID.** Optional acoustic verification, with your own free key.
- **LRCLIB.** Lyrics. Free, no key, community-run, and we pace politely.
- **YouTube.** Where the audio actually comes from, through yt-dlp.

_Not legal advice. I'm a person who read the licenses carefully, not a lawyer.
If you plan to redistribute spotdl+ commercially, get somebody qualified to
look._
