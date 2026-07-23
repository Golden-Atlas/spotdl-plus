# spotdl+ command reference

Every command, every argument, and the archival workflow they add up to.
`spotdl+ -h` (or `-help`, `--help`) works on the app and on every command.
Exit codes everywhere: `0` success, `1` refused or config error, `2` finished
with failures, `3` parked.

---

## Getting music

### `spotdl+ get <source>` - the main verb
Resolve, filter, dedupe, match, **show the plan**, confirm, download, verify.
Nothing downloads before you see the track count and the estimated size.

| argument | meaning |
|---|---|
| `<source>` | a Spotify URL or URI (artist, album, playlist, track), a typed search (`artist:Duster`, `album:OK Computer`, `song:Sunsetz`, and `track:` works too), or a bare name like `Creep` that gets scored across all three kinds and *asks* when two readings tie |
| `-p, --profile` | what "an artist" means. `canonical` is the default: albums, EPs, and singles, with live records, compilations, and karaoke excluded on MusicBrainz facts, and reissues collapsed to the original master. `completionist` keeps everything including appears-on and both masters. `studio` is albums only. |
| `-y, --yes` | accept the plan without the confirmation prompt, for scripts |
| `-v, --verbose` | per-match scores, filtered-release reasons, retry chatter |
| `--to` | save in a device's format: `ipod` (AAC .m4a), `ipod-lossless` (Apple Lossless .m4a), `universal` (MP3), or `archive` (your configured format, the default). Overrides the `delivery` config key for one run. |
| `--lyrics-omit` | skip lyrics for this run. They're on by default, time-synced from LRCLIB and embedded in every file. |
| `--lyrics-plain` | prefer plain text over time-synced |
| `--lyrics-sidecar` | also write a matching `.lrc` file next to each track |

### `spotdl+ sync <source>` - bring a source up to date
Takes the same forms as `get`, for things you've gotten before. New tracks
download, owned tracks skip, and what the source contained is remembered so next
time knows what changed. It's explicit on purpose. You call it, it syncs, and
nothing ever runs in the background.

| flag | meaning |
|---|---|
| `--diff` | look and don't touch: `+4 new, 213 unchanged, 2 gone`. Downloads nothing, deletes nothing. |
| `--prune` | **deletes** the files for tracks that left the source since last time, which is the playlist workflow. Every file gets named as it goes, and a track another synced source still wants gets spared. |
| `-y / -v / -p / --to` | same as on `get` |

### `spotdl+ plan <source>` - price it first
Everything `get` does *except* the download. Resolves, filters, dedupes,
matches, and reports "N tracks, ~X MB, Y owned, Z unmatched". Same `-p/--profile`
and `-v`. Costs searches and zero bytes.

### `spotdl+ resume` - nothing is ever lost
Picks the most recent parked or interrupted run up where it stopped. Tracks
already fetched don't get refetched, and a run killed mid-album finishes the
album.

| flag | meaning |
|---|---|
| `--retry-failed` | give FAILED tracks fresh attempts too, rewinding them to their restartable stage |
| `--wait` | babysit mode. If the run parks on a rate limit it sits out the cooldown and resumes itself, up to 6 cycles, so an overnight run finishes without you. Ctrl+C works the whole way through. |
| `--wait-minutes N` | how long each sit-out lasts (default 30) |
| `-v, --verbose` | as above |

---

## Trust and repair

### `spotdl+ audit` - hold the library to account
Six claims per owned file. **present** means it exists and isn't empty. **sound**
means it probes and its duration matches the metadata. **tagged** covers title,
artist, album, albumartist, tracknumber, date, and ISRC, demanding only what the
metadata actually knows. **pictured** means embedded art when the album has any.
**accounted** means nothing sits in the library folder that we don't know about.
And with `--identity`, **confirmed** means the audio is acoustically the
recording it claims to be.

| flag | meaning |
|---|---|
| `--fix` | repair in place. Missing tags and art get rewritten from the metadata blobs stored at discovery, and audio bytes never get touched. Also writes a `cover.jpg` into every album folder missing one, since players that can't read embedded opus art use those. |
| `--deep` | fully decode every file, about 0.5s each, which catches mid-stream corruption a header probe passes |
| `--identity` | the sixth claim. Implies `--deep` and needs `acoustid_api_key`. Fingerprints every file against AcoustID. CONFIRMED counts, UNKNOWN flags without failing since bedroom releases are honestly absent from the database, and a positive MISMATCH always reports. Under `--fix` a mismatch **quarantines**: the file moves whole to `<library>/.quarantine/`, its ownership gets revoked, and the track requeues for a fresh match. |
| `--show N` | how many issues to list per kind (default 20) |

### `spotdl+ relink [<track>]` - overrule the matcher
The persisted scoreboard for a track, the winner and every loser with the reason
it lost, rendered for your judgment.

| argument | meaning |
|---|---|
| `<track>` | part of a title or artist, or a track id. Several hits prompt a pick. |
| `--url <u>` | force a source we never even found |
| `-y, --yes` | take the first search hit without asking |
| `--now / --later` | download right after relinking (the default) or just queue it |
| `--queue` | **triage mode**. Walks every track waiting on a matching verdict, meaning refusals and ties, across all runs in one sitting. Per track: `c` opens the full candidate detail (score with its per-signal breakdown, length against what we wanted, size, why it lost, link), `1-8` picks, `p` previews one in your browser so you can listen first, `r` searches youtube yourself (top 5, full detail, pick or preview or re-search), `u` pastes a URL, `s` skips, `x` abandons, `q` quits keeping decisions. Every path backs out with `b` so nothing is a dead end. Downloads happen once at the end. |
| `--auto 0.65` | no sitting at all. Every queued track whose best candidate scores at least the bar gets relinked to it automatically, leaving only the real judgment calls. |

Relinking revokes the library row, because a distrusted file can never justify a
skip. Refusals persist their full scoreboard too, so there's always something to
read.

### `spotdl+ library` - what you own
Counts, total size, per-artist table.

| flag | meaning |
|---|---|
| `--verify` | confirm every owned file is still on disk, and list the missing |

### `spotdl+ export` - make an iPod copy of your library
Transcodes your owned files into a device format and writes the copies to a
folder, or straight onto an iPod's drive. It **never re-downloads**, since it
works entirely from the archive you already have, and the archive is left alone.
Safe to re-run, because anything already at the destination gets skipped, so a killed
export picks up where it stopped and adding one song then re-exporting copies
one song. Drag the result into the Apple Music or iTunes app to sync.

| argument / flag | meaning |
|---|---|
| `-P, --profile` | a named device: `ipod`, `ipod-lossless`, `mp3-player`, or one you made. `--profile list` shows them all and walks you through making new ones. A profile without a saved destination asks once and remembers, and after that the whole export is one flag. |
| `-d, --dest` | folder or iPod drive letter to write the copies into. A profile can carry this for you. |
| `--to` | device format: `ipod` (AAC .m4a, the default), `ipod-lossless` (Apple Lossless), or `universal` (MP3) |
| `<query>` | optional, only export owned files whose path contains this. `export -d E:\ pierce` gets you just Pierce The Veil. |
| `--force` | re-export even files already at the destination |

### `spotdl+ cleanup` - housekeeping in one verb
The quick pass takes seconds and touches no music. Stale cache artifacts, torn
`.part` files, ghost ownership (owned on paper and gone on disk, healed so it
can download again), empty folders folded, run db compacted.

| flag | meaning |
|---|---|
| `--deep` | probe every owned file for audio, tags, and duration, and repair in place. Minutes, not seconds. |
| `--datacheck` | rewrite every file's tags fresh from the stored metadata so drift dies, and rebuild `genres.json`. Pairs with `--deep` for the full works. |
| `--covers` | backfill missing folder `cover.jpg` files from the embedded art |
| `--empty-quarantine` | actually delete `.quarantine`'s contents, because it never empties itself |
| `-y, --yes` | skip the confirmations |

### `spotdl+ move-library <dest>` - relocate everything
Files move, every ownership record re-anchors, nothing re-downloads, and
`search`, `audit`, and `export` all follow to the new address. It asks whether
future downloads should land there too, and warns you plainly if you say no.

### `spotdl+ undo` - take back the last run
The most recent run unwinds whole: the files it placed, the ownership it
claimed, its bookkeeping. Resumes and relinks reuse the same run so they unwind
with it. Anything owned *before* that run stays put. This is for the wrong
artist, oops moment, and everything it removes is one `get` away.

### `spotdl+ uninstall` - remove what spotdl+ put here, and nothing else
Plain, it clears only what regenerates itself: the download cache and the tools
we fetched. Your config, ownership records, and music stay, so reinstalling
picks up where you left off.

| flag | meaning |
|---|---|
| *(none)* | cache and tools go. Everything else stays. This is the one you want between reinstalls. |
| `--purge` | also the config and the ownership database. Your music stays on disk, but spotdl+ forgets it owns any of it, so the next run downloads it all again. |
| `--everything` | also every file spotdl+ downloaded. **Only those.** Anything that was in that folder before you pointed us at it is left alone, and we print how many we spared. |
| `--dry-run` | show what would go, touch nothing |
| `-y, --yes` | skip the confirmation |

None of it removes the program. Use Add/Remove Programs for that, which also
takes the PATH entry and the shortcuts with it.

### `spotdl+ tidy` - compact the run database
Prunes bookkeeping for long-finished runs and reclaims the space. Never anything
still holding failed tracks, because that's your relink queue. Never touches music.

---

## When something needs explaining

### `spotdl+ status` - where the last run stands
Per-state counts (done, matched, fetched, failed, and so on) and estimated size.

| flag | meaning |
|---|---|
| `--errors` | every failure with its full message **and its remedy** |

### `spotdl+ stats` - your library in numbers
Tracks, size, ~hours of music, artists, formats, per-artist table. `-d` goes
deeper: every artist, biggest albums, the last 7 days, newest additions.

### `spotdl+ search <query>` - find what you own
Instant and offline. Matches title, artist, or album and shows the deep info per
hit, meaning album, year, format, size, length, and path. `-c` flips it to album
completeness: `✓ complete` or `○ partial, 3 short`, per album, judged against
everything any run has ever seen.

### `spotdl+ report` - the "it broke, here" block
One pasteable block about the last run: versions, environment, per-state counts,
failures grouped by code. `--list` dumps every failure in dev terms, one JSON
line each with full context, ready to send to whoever's debugging.

### `spotdl+ explain <CODE>` - what an error means
Any code from `status --errors`, so `RATE_LIMITED`, `MATCH_AMBIGUOUS`,
`NET_TLS_UNTRUSTED`, and the rest. You get its retry policy and what a human
should actually do. An unknown code lists every code that exists.

### `spotdl+ doctor` - check everything up front
Eleven named checks: Spotify credentials, ffmpeg, ffprobe, yt-dlp, deno (the JS
runtime YouTube demands), the PO-token provider, fpcalc, the AcoustID key,
library writability, free disk (the run gate keeps a 2 GB reserve), and live
network. Below that, the entire resolved config **with the provenance of every
value**, meaning default, user config, project config, env, or CLI. Advisory
checks (PO tokens, fpcalc, AcoustID) warn without blocking, because their absence
degrades verification and not downloads.

Add `--network` and it also pulls one real track from YouTube, which proves
whether this machine can download *right now*. That's the fast way to tell a
rate-limited IP or an unreadable cookie store apart from a tooling problem. If
your browser cookies can't be read, because the browser is open or Chrome
encrypted them, it says so and notes the run would just proceed without them.

`--network` also uses your Spotify credentials for real: it mints a token,
reads an album, and reports which step died, because the plain `credentials`
check above only proves two strings exist. An app that was never set up for the
Web API still has an id and a secret, and it will sail through that line while
every request it makes gets refused.

Playlists are their own permission story, so it reads one too. By default that
is a small public playlist kept alive for this check, and a refusal there means
your app, not your network. If that playlist ever stops resolving you get a
warning rather than a failure, because somebody deleting it says nothing about
your setup, and the warning tells you to re-check with `--playlist "<your own
playlist link>"`. Pass that flag any time you want a specific playlist tested.

It is not self-sourced on purpose. Searching for a playlist to test with returns
Spotify's own editorial ones, and those are unreadable through the Web API for
everybody now, so a search-driven check fails on a perfectly healthy machine.

Add `-v` on top of `--network` and each layer gets timed separately: DNS, TLS
and HTTPS forced to IPv4 which is the route downloads actually use, and the
canary download itself. "It's slow" finally says WHERE.

### `spotdl+ setup` - connect your Spotify account
Walks you through making a free Spotify app and pastes the client id and secret
into your config. Takes about a minute. It fires on its own the first time a
command needs the network and finds no credentials, but only on a real terminal,
so a script gets the typed error instead of a prompt that would hang forever.

### `spotdl+ welcome` - the friendly first run
What the installer opens when it finishes, and what the Start Menu shortcut
points at later. Connects Spotify, then shows in plain terms what to type next.
Written to stand on its own for somebody who has never opened a terminal.

### `spotdl+ version`
Which spotdl+ this is.

---

## Configuration (read once at startup, and `doctor` shows what won)

Precedence: defaults, then `%LOCALAPPDATA%\spotdlplus\config\config.toml`, then
`./spotdl+.toml`, then environment, then CLI flags.

| key (env var) | default | meaning |
|---|---|---|
| `spotify_client_id` / `spotify_client_secret` (`SPOTIFY_CLIENT_ID/_SECRET`) | none | your Spotify app credentials |
| `acoustid_api_key` (`ACOUSTID_API_KEY`) | none | application key for identity verification |
| `youtube_cookies_from_browser` (`SPOTDLPLUS_COOKIES_FROM_BROWSER`) | none | borrow YouTube cookies from a browser (chrome, firefox, edge, brave, and so on). Clears age-gates and most bot walls, and it's the biggest reliability lever for big runs. |
| `mass_block_streak` (`SPOTDLPLUS_MASS_BLOCK_STREAK`) | `10` | how many downloads YouTube can block in an unbroken row before the run parks with its queue intact instead of failing everything. `0` disables it. |
| `output_dir` (`SPOTDLPLUS_OUTPUT_DIR`) | `~/Music/spotdl+` | the library root. Nothing is ever written outside it. |
| `audio_format` (`SPOTDLPLUS_FORMAT`) | `opus` | opus, mp3, flac, m4a, wav, alac. Same-codec fetches get stream-copied, so zero generational loss. |
| `bitrate` (`SPOTDLPLUS_BITRATE`) | `192k` | for re-encodes only |
| `delivery` (`SPOTDLPLUS_DELIVERY`) | `archive` | a friendly format preset. `archive` keeps `audio_format`, `ipod` is AAC .m4a at 256k, `ipod-lossless` is Apple Lossless, `universal` is MP3 at 320k. Set it once and every download saves in that format. `--to` overrides per run. |
| `profile` (`SPOTDLPLUS_PROFILE`) | `canonical` | default selection profile |
| `template` (`SPOTDLPLUS_TEMPLATE`) | `{album_artist}/{album} ({year})/{track_tag} {title}.{ext}` | fields: artist, artists, album_artist, album, title, year, track_no, disc_no, track_tag, isrc, ext. Every segment gets NTFS-sanitized, metadata can't smuggle in separators, extensions survive any length squeeze, and the whole path stays under 240 chars. |
| `concurrency` (`SPOTDLPLUS_CONCURRENCY`) | `2` | worker threads. More is mostly a way to get rate-limited. |
| `confidence_floor` | `0.72` | below it, skip instead of guessing |
| `lyrics` (`SPOTDLPLUS_LYRICS`) | `synced` | `synced` embeds time-synced lyrics with plain as the fallback, `plain` prefers unsynced, `off` skips them |
| `lyrics_sidecar` (`SPOTDLPLUS_LYRICS_SIDECAR`) | `false` | also write a `.lrc` next to each track |
| `limit_rate` (`SPOTDLPLUS_LIMIT_RATE`) | none | cap download speed (`2M`, `500K`, or plain bytes/s) for runs on shared wifi. Unset means full speed. |
| `youtube_cookiefile` (`SPOTDLPLUS_COOKIEFILE`) | none | path to a cookies.txt, for when you'd rather export cookies by hand than let us read a live browser profile. `youtube_cookies_from_browser` is the easier one. |
| `cache_dir` (`SPOTDLPLUS_CACHE_DIR`) | `%LOCALAPPDATA%/spotdlplus/cache` | where downloads and transcodes wait before they earn a place in the library. Point it at a fast disk if your library lives on a slow one. |
| `state_dir` (`SPOTDLPLUS_STATE_DIR`) | `%LOCALAPPDATA%/spotdlplus/state` | where the run database lives. `-o` moves this with the files, so each vault owns its own. |
| `[export_profiles.<name>]` | none | named export targets (`to`, `dest`). See `export --profile`. |
| `max_attempts`, `backoff_*`, `batch_size`, `lease_s` | sane | Engine retry and queue tuning |

---

## Building an archive one link at a time

```
spotdl+ doctor                      # once per machine, and after anything breaks
```

Then, per link, forever:

```
spotdl+ get <paste the spotify link>       # artist, album, playlist, or track
     -> read the plan (count, size, owned) -> y
     -> watch it verify each track into D:\Music
```

That's the whole loop. Everything else is for the exceptions.

- **It printed ✗ lines?** `spotdl+ status --errors` names each one with a
  remedy and `spotdl+ explain <CODE>` elaborates. Refusals (`MATCH_NONE` and
  `MATCH_AMBIGUOUS`) are the matcher declining to guess, so queue them up and
  run `spotdl+ relink --queue` when you've got ten minutes of judgment to spend.
- **Interrupted?** Power, sleep, Ctrl-C, full disk, dead wifi. Run `spotdl+
  resume`. Finished tracks stay finished and fetched-not-placed tracks don't
  refetch.
- **Same link twice?** Free. Ownership is by recording, meaning ISRC, so
  re-running a playlist after adding one song downloads one song, and an artist
  re-run after a new single downloads the single. `spotdl+ sync <link>` is the
  sharper tool for it, since it also remembers what the source contained, shows
  a `--diff`, and `--prune`s what left a playlist if you ask.
- **Trust check, monthly or whenever.** `spotdl+ audit --identity --fix` runs
  all 6 claims, quarantines anything that went wrong, and repairs the
  repairable, then `resume` fetches replacements. `spotdl+ cleanup` handles the
  boring dirt, meaning stale cache, ghost claims, and empty folders, in seconds.
