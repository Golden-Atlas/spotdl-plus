# spotdl+

A music archival pipeline that remembers what it was doing.

Point it at anything on Spotify, whether that's an artist, an album, a playlist,
a track, or just a name, and it builds you an organized, fully tagged local
library with every decision it made written down where you can check it.

```
spotdl+ get "https://open.spotify.com/artist/..."   # a whole discography
spotdl+ get "artist:Duster"                          # or by name
spotdl+ get "Creep"                                  # it figures out what you meant
```

## Why this exists

Every tool in this space forgets. It forgets where it was when the network
dropped, forgets why it picked the recording it picked, and forgets it already
downloaded a song when that song shows up again on a second release.

spotdl+ keeps its work in SQLite instead of memory. Every track is a resumable
state machine, so it doesn't redo work, doesn't lose a run, and can always tell
you why it did something.

Six things it does differently, all of them checked against the live services:

**It doesn't trust Spotify's idea of a discography.** Spotify calls live albums,
compilations, and remix collections all `album`. MusicBrainz release-groups
carry the real answer, so `--profile canonical` can drop them on a fact instead
of a guess. Radiohead came back as 59 releases and kept 40, every exclusion
correct and announced.

**Dedupe runs in two tiers.** The same recording on a single and on its album
shares an ISRC, so it collapses in the database schema itself. A remaster is a
different recording of the same work with its own ISRC (`Airbag` 1997 and 2017
share zero), so a second pass clusters those and keeps whichever master you
asked for. 217 Radiohead tracks in, 178 queued.

**Matching shows its work.** Duration carries the most weight, since a live take
really is longer and a sped-up edit really is shorter. Negative keywords drag a
score down, and every candidate gets saved with the reason it won or lost. A bad
match is a scoreboard you open with `spotdl+ relink`, not a mystery you re-run.

**It refuses below the confidence floor.** A missing song is a gap you can see.
A wrong song that looks right corrupts the archive quietly.

**Nothing half-written reaches the library.** Downloads and transcodes stay in a
cache. The final move is an fsync and an atomic rename on the destination
volume, and a separate verify stage hashes the file and probes its duration
before anything gets recorded as owned.

**The core never prints.** Everything is a typed event on a bus and the CLI is
one subscriber. A GUI would be another one, which nobody has written yet.

## Install

**Windows, the easy way.** Grab the installer from
[Releases](https://github.com/Golden-Atlas/spotdl-plus/releases) and run it. It
carries its own ffmpeg, ffprobe, deno, and fpcalc, so there's nothing else to
install. The first run opens a window that connects your Spotify account and
shows you what to type. It's unsigned, so SmartScreen warns you once: More info,
then Run anyway. Getting rid of that warning needs a code-signing certificate I
don't have.

**From source, any platform:**

```
pip install -e .
spotdl+ doctor        # tells you what's missing and how to fix it
```

That way you need **ffmpeg** on PATH, and because modern YouTube demands it, a JS
runtime (`doctor` looks for a self-provisioned `deno.exe` in the app's tools
dir) plus the bgutil PO-token script. `doctor` checks each layer separately, so
when one breaks you know which one.

Either way you'll want your own Spotify app for the client id and secret, from
developer.spotify.com/dashboard. One catch worth knowing up front: as of early
2026 Spotify moved dev-mode API access behind Premium, so the account that makes
the app has to be a paying one. That is their call and not mine, and it is the
real cost of admission here. `spotdlp setup` walks through the app itself and it
takes about a minute. When you make it, tick **Web API** and give it any
redirect URI, because an app without those still hands you keys and then refuses
every playlist you ask for.

Credentials go in the user config, never in the repo:

```toml
# %LOCALAPPDATA%/spotdlplus/config/config.toml
spotify_client_id = "..."
spotify_client_secret = "..."
output_dir = 'D:/Music'
audio_format = 'opus'     # youtube-native: same-codec fetches are stream-copied,
bitrate = '192k'          # zero generational loss
```

## Commands

**Getting music**

| command | what it does |
|---|---|
| `get <source>` | resolve, filter, dedupe, match, **show the plan**, download |
| `plan <source>` | all of that minus the download, so you can price a run first |
| `sync <source>` | bring a source up to date: fetch what's new, `--diff` to preview, `--prune` to drop what left a playlist |
| `resume` | pick a parked or interrupted run up where it stopped (`--retry-failed`, `--wait`) |
| `export --profile ipod` | copy the library into a device format, offline and non-destructive |

**Trust and repair**

| command | what it does |
|---|---|
| `audit --fix` | make every owned file prove itself, `--identity` adds acoustic confirmation |
| `relink <track>` | the match scoreboard, and override it by hand (`--queue`, `--auto 0.65`) |
| `cleanup` | stale cache, ghost bookkeeping, empty folders, `--deep --datacheck` for the thorough pass |
| `move-library <dest>` | relocate everything, bookkeeping follows and nothing re-downloads |
| `undo` | take back the most recent run, files and all |

**When something needs explaining**

| command | what it does |
|---|---|
| `doctor` | check everything up front (`--network -v` times each layer against YouTube) |
| `status --errors` | every failure with its remedy attached |
| `stats` / `library` | the numbers, and who's in there |
| `search <query>` | find what you own, instantly and offline (`-c` for album completeness) |
| `explain <CODE>` | what an error code means and what to do about it |
| `report` | one pasteable block about the last run, for when you need to show someone |

Every flag is in [COMMANDS.md](COMMANDS.md).

## For your iPod

The archive keeps everything as opus, which is small and sounds like the source.
When you want it on an iPod you don't re-download. `spotdl+ export --to ipod
--dest E:\Music` transcodes your library into AAC `.m4a`, or `--to
ipod-lossless` for Apple Lossless, and writes the copies wherever you point it.
The archive is left alone. Drag the result into the Apple Music or iTunes app to
sync. You can also set `delivery = "ipod"` once and every download saves in iPod
format from the start, which the first-run setup asks about. Re-running is safe
since only what's new gets copied.

## Profiles

What "give me this artist" actually means:

- `canonical` *(default)*: studio albums, EPs, singles. Live records,
  compilations, karaoke, and DJ mixes get excluded on MusicBrainz facts, never
  on title-string guessing. Reissues collapse to the **original master**.
- `completionist`: everything, including appears-on features and both masters of
  every reissue. The honest hoard.
- `studio`: albums only.

Features file under the album's owner and credit everyone. `Make Me Wanna` goes
in `Babeheaven/` with `ARTIST=Babeheaven, Navy Blue`.

## Layout

```
D:/Music/
  Pretty Sick/
    home2hide (2026)/
      01 home2hide.opus     <- title, artists, album, dates, label, ISRC, UPC,
                               MusicBrainz ids, and cover art, all embedded.
                               The file survives the database.
```

Template: `{album_artist}/{album} ({year})/{track_tag} {title}.{ext}`. Every
segment gets sanitized for NTFS, meaning illegal characters, reserved device
names, and length, and metadata can never smuggle in a path separator.

## When YouTube breaks, and it will

Downloads stalling on read timeouts while search still works is almost never
your code. In order of likelihood: a broken IPv6 route (we force IPv4 by
default), antivirus TLS interception (we verify through the OS trust store and
never skip verification), a missing JS runtime, or missing PO tokens. `spotdl+
doctor` checks all four layers by name and `ARCHITECTURE.md` explains each one.

Getting **blocked** on a big artist, meaning bot walls and age-gates, is a
different animal. Set `youtube_cookies_from_browser = "chrome"` (or firefox, or
edge) in config and it lends the download your browser's YouTube cookies, which
clears most of it. If a whole run still gets throttled, spotdl+ parks it with
the queue intact and tells you to `resume --retry-failed` after a break. Nothing
is lost.

## The contract

`ARCHITECTURE.md` isn't documentation, `tests/test_architecture.py` enforces it.
Imports flow strictly downward, the core has zero third-party dependencies,
nothing outside `cli/` may print or import a renderer, every error carries a
remedy, and no stage may sleep or retry since the Engine owns all timing. When
the code and the contract disagree the suite goes red, which is the point.

Every number in this README came out of a real field-test set, 20 sources from
Radiohead down to bedroom projects with one EP and an album titled `❤︎`. The
long tail is where matching, ISRC coverage, and release classification actually
break. The happy path proves nothing.

## Legal posture

This tool talks to services whose terms restrict downloading, and whether your
use is appropriate is yours to judge. I built it to archive music I have the
rights to keep. It ships without stream ripping from Spotify itself, because that's
DRM and circumventing it isn't a feature, and without anything that evades an
access control. Lyrics come from [LRCLIB](https://lrclib.net), a free community
database, because that one turned out to have an honest source.

Rate limits get obeyed to the second. MusicBrainz is paced under 1 request per
second, which is stricter than what they publish, on purpose.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).

That's arithmetic, not preference. spotdl+ imports
[mutagen](https://github.com/quodlibet/mutagen), which is GPL-2.0-or-later, to
write tags, and the Windows installer bundles a GPL-3.0 build of FFmpeg. Link a
GPL library and ship the result and the result is GPL. Picking MIT here would
have been a license violation wearing a friendly hat.
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) is the full accounting of
what's bundled, under what terms, and where to get its source.

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md) has the setup, the five rules the test suite
enforces, and an honest list of what I'd probably say no to. Read the style
notes there before you write any prose. The tool sounding like one program is a
feature.

Security reports go through a
[private advisory](https://github.com/Golden-Atlas/spotdl-plus/security/advisories/new),
not a public issue. Details are in [SECURITY.md](SECURITY.md).
