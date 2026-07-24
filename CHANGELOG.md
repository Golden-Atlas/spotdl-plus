# Changelog

Every shipped build's exact installer lives in `releases/<version>/` with a
SHA-256 and a build note. This file is the readable history of what changed.
Newest first.

## 1.2.2 - groundwork for the free path

Mostly under the hood, and nothing a keyed run does changes. This is the work
toward running spotdl+ without a paid Spotify app, which matters more now that
Spotify stopped letting newly-made apps read playlists at all in early 2026.

- **The free sign-in reads end to end, opt-in.** Set `spotify_auth = "anon"` and
  it pulls albums, tracks, artists, playlists, and search the way the web player
  does, no account and no Premium. It talks to Spotify's own web API instead of
  the endpoints Spotify deleted, and keys tracks on their Spotify id, so full
  ISRC dedupe still wants your own key. `spotdlp doctor --spotify-web` proves the
  whole path on your own network.
- **`doctor` stopped giving backwards advice about playlists.** A friend on a
  fresh app kept getting playlists refused, and the remedy said "make a new app",
  which is the exact thing that breaks it. Spotify locked new apps out of
  playlists in Feb 2026 and grandfathered the old ones, so a fresh app cannot
  help. It says that now, and points at a pre-2026 app or the free path instead.
- **`spotdlp -v` lists every command, grouped**, and `--help` got tighter and
  more accurate.

Nothing on the keyed download path changed. 562 tests, green.

## 1.2.0 - the living archive, whole

The capstone. 1.1.3 through 1.1.8 shipped one piece at a time and this rolls
them into the release that changes what the tool IS, from a downloader you run
into a library that stays alive. Six versions, short version:

- **It stays current.** `sync`, with `--diff` to preview and `--prune` for
  playlists (1.1.6)
- **It knows itself.** `stats`, `search`, `report`, layered `doctor --network
  -v`, and a run bar with an honest ETA (1.1.3)
- **The files got richer.** Time-synced lyrics, real genres plus `genres.json`,
  compilation flags (1.1.4)
- **Judging matches got humane.** Full-detail picker, previews, your own youtube
  searches, `relink --auto` (1.1.5)
- **It cleans up after itself.** `cleanup` with its deep gears, `move-library`,
  `undo`, `tidy` (1.1.7)
- **Devices got names.** Export profiles (1.1.8)

Plus the quiet levers. `resume --wait` babysits rate-limit cooldowns,
`limit_rate` caps bandwidth, and setup offers AcoustID out of the box now.

And the final polish, earned by a full real-machine test session over a
3,760-track library:

- **No more invisible questions.** The live display hands the terminal back the
  moment the plan prints, so `sync` and `get` confirm prompts actually show up
  instead of hiding under the progress bar looking like a hang. One of them did
  exactly that for a whole evening.
- **`ERROR: ERROR:` is finally dead.** yt-dlp colors its error prefix on a real
  terminal, and those invisible color codes had been defeating the cleanup for
  two releases while every piped test looked fine. Colors get stripped first
  now. Lesson filed.
- **`stats` counts artists the way `library` does**, by the record's owner.
  Grouping on the full feature list once turned one library into 543 'artists',
  one per collab combination, which read as absurd because it was.
- **The relink picker says when a track has no receipts.** Older refusals
  predate the scoreboard, so it points you at `r` or `u` instead of shrugging.
- **Exported copies keep their words.** `export` rebuilt every tag from stored
  metadata but never carried the lyrics across, so device copies showed up
  wordless while the archive had them the whole time. It reads them back out of
  the archived file now, same as it already did for cover art.
- One voice everywhere. Prompts, headers, checklists, and counts all speak the
  same way across every command, and "1 tracks" is grammatically extinct.

Late additions, all of them found by running the shipped .exe against a real
machine instead of trusting the test suite:

- **`uninstall`, in three sizes.** Plain clears the cache and the fetched tools
  and leaves your config, ownership, and music alone, so a reinstall plugs
  straight back in. `--purge` also drops the config and the ownership database.
  `--everything` also deletes the files we downloaded, and only those.
- **Pointing `output_dir` at a folder you already use is safe now.** A rendered
  path can land exactly on a file that was there first, and placement is an
  atomic replace, so it used to overwrite without a word. We only replace a file
  a library row claims as ours. Anything else stops with `FS_WOULD_OVERWRITE`
  and tells you what to do.
- **The installer stopped duplicating your PATH.** A `Check:` argument does not
  expand constants in Inno, so the guard compared PATH against the literal text
  and appended another copy on every single install. Six on my own machine. It
  now repairs what the old one did, keeps your entry where you put it, and the
  uninstaller removes all the copies rather than one.
- **`doctor` stopped saying yes without asking.** The credentials line only
  ever proved two strings were present, so somebody whose Spotify app was never
  set up for the Web API got an all-green screen while every request they made
  was refused. `--network` now mints a real token and reads an album, and
  it reads a playlist as well, since that is a separate permission and the one
  that actually catches a misconfigured app. `--playlist <url>` points it at a
  specific one.
- **A 403 from a metadata provider stopped giving YouTube advice.** Spotify
  refusing a playlist used to raise the same error as a YouTube bot wall, so the
  remedy told people to configure browser cookies they had already configured.
  Now it says what it is, and it no longer trips the circuit breaker, because one
  playlist we may not read is not the host being down.
- **`stats` was hiding files.** It inner-joined the library against the run
  bookkeeping, and `tidy` prunes that bookkeeping while ownership correctly
  survives, so every tidied file dropped out of the artist count and the hours.
  My archive read 135 artists where it has 158, with 212 files invisible.

Nothing from 1.1.x changed underneath. Every piece shipped, tested, and verified
on a clean machine as it landed. 476 tests, all green.

## 1.1.8 - the travel kit

- **Export profiles.** Your devices get names. `spotdlp export --profile ipod`
  just works, and `--profile list` shows every profile (the built-ins are ipod,
  ipod-lossless, and mp3-player) and walks you through making your own with a
  name, format, and destination. The first time a profile runs without a saved
  destination it asks once and remembers, so from then on the whole export is
  one flag. Profiles live in config.toml as plain [export_profiles.name] tables
  you can edit by hand.

## 1.1.7 - the custodian

The library learns to take care of itself. Three verbs, all careful, all loud
about what they touch.

- **`cleanup`** does housekeeping in seconds: stale cache artifacts, torn .part
  files, ghost ownership (owned on paper and gone on disk, healed so it can
  download again), empty folders folded, run db compacted. Then the deeper
  gears. `--deep` probes every owned file for audio, tags, and duration and
  repairs in place. `--datacheck` rewrites every file's tags fresh from stored
  metadata so drift dies, and rebuilds genres.json. `--covers` backfills missing
  folder art from the embedded copies. `--empty-quarantine` finally deletes what
  quarantine has been keeping as evidence.
- **`move-library <dest>`** relocates the whole library. Files move, every
  ownership record re-anchors, and nothing re-downloads. It asks whether future
  downloads should follow, and warns you plainly if you say no.
- **`undo`** takes back the most recent run whole: the files it placed, the
  ownership it claimed, its bookkeeping. Anything owned before that run stays
  exactly where it was. This is for the wrong artist, oops moment.

## 1.1.6 - the living archive

The library learns to stay current instead of only growing. One new verb, and
the memory that makes it mean something.

- **`sync`** points at anything you've gotten before, taking the same forms as
  `get`, and brings that source up to date. New tracks download, owned tracks
  skip, and what the source contained is remembered for next time. You call it
  explicitly like get, because there's no background anything.
- **`sync --diff`** is the look-don't-touch mode: `+4 new, 213 unchanged, 2
  gone`. Nothing downloads and nothing deletes.
- **`sync --prune`** is for playlists. It DELETES the files for tracks that left
  the source since last time. Every file gets named as it goes, and a track
  another synced source still wants gets spared, and it says which ones. This
  one actually deletes, which is the point of it.
- **Snapshots, quietly.** Every real `get` or `sync` walk records what the
  source contained. That's the whole backend. No new commands to learn, it just
  makes `sync` smart the second time.

## 1.1.5 - the judge's bench

`relink` grows up. Deciding a match used to mean squinting at a table and
hoping. Now the picker gives you everything and clears the easy calls itself.

- **Search youtube yourself, from inside the picker.** `r` takes any search you
  type and shows the top 5 with the same full detail the scoreboard has, meaning
  length against what we wanted, channel, views, and link, then lets you pick,
  preview, re-search, or back out. No more leaving the terminal to go hunt a
  URL.
- **Preview before you commit.** `p` opens any candidate in your browser so you
  can actually LISTEN. Every path backs out cleanly and there are no dead ends.
- **`relink --auto 0.65`.** No sitting at all. Every queued track whose best
  candidate clears the bar gets relinked to it, leaving only the real judgment
  calls for a human.
- **Refusals keep their receipts now.** A track we refused to guess on used to
  persist NO scoreboard, so the picker stared at an empty table and `--auto` had
  nothing to read. The full scoreboard rides along on every refusal now, and
  losing candidates carry their real scores. They used to all read 0.00, which
  was a small lie.

## 1.1.4 - the song itself

The file gets richer. Everything here rides inside the audio, so it survives the
database, the export, and the decade.

- **Lyrics, time-synced, on by default.** Every download looks its words up on
  LRCLIB, which is free and needs no key, and embeds them. Synced when they have
  it, plain as the fallback, and never a failure when they don't.
  `--lyrics-omit` skips them, `--lyrics-plain` prefers unsynced, and
  `--lyrics-sidecar` also drops a matching .lrc next to each track. The config
  keys `lyrics` and `lyrics_sidecar` set the default once.
- **Genres, finally real.** Tracks borrow their artist's Spotify genres, one
  cheap lookup per artist and memoized, and carry them in tags. Plus a
  `genres.json` in the library root with a section each for songs, albums, and
  artists, rebuilt after every run.
- **Compilations group right.** Various-artists records get the compilation flag
  in every format, so an iPod shows ONE album instead of 20 one-track ones.
- **Setup asks about AcoustID.** One optional question, a one-line explanation,
  and it opens the free key page, so acoustic verification works out of the box
  for anybody who wants it.

## 1.1.3 - the instruments

Read-only surfaces and small levers. Nothing about downloading changed. This is
the release where the tool learns to answer questions about itself.

- **`stats`** is your library in numbers: tracks, size, ~hours of music,
  artists, formats. `-d` goes deeper into every artist, biggest albums, and the
  last 7 days.
- **`search`** finds what you own, instantly and offline. `-c` flips to album
  completeness, so complete or partial and how many tracks short.
- **`report`** is one pasteable block about the last run, for "it broke, here"
  messages. `--list` dumps every failure in dev terms, one JSON line each.
- **`doctor --network -v`** times each layer separately, being DNS, TLS and
  HTTPS, and the download itself, so "it's slow" finally says WHERE.
- **`resume --wait`** babysits itself. Park, cool down ~30 min, resume, repeat,
  up to 6 cycles. A big overnight run finishes without you.
- **A loose ETA on the run bar.** By-step and honest about being rough.
- **`limit_rate`** caps download speed ('2M', '500K') in config, for runs on
  shared wifi. Unset means full speed, like always.

## 1.1.2 - the audit release

A full functional audit of every command, every module, and every failure path,
then fixes for the four real defects it found. Nothing about a healthy run
changes.

- **Resuming an old queue can't re-download your library.** Ownership gets
  rechecked right before each download now instead of only at planning time.
  Resuming a stale parked queue once re-fetched ~230 songs that were already on
  disk. A track you deliberately `relink` still re-downloads, because that's the
  point of it.
- **A busy machine can't permanently fail a track.** ffmpeg or ffprobe getting
  killed for taking too long, which happens when the system is slammed mid-run,
  used to count as a final verdict. It retries automatically with backoff now
  (`TOOL_HUNG`).
- **`spotdlp tidy`.** The run database grows with every run and one real one hit
  215 MB. Tidy prunes bookkeeping for long-finished runs and compacts the file,
  never touching your music, what you own, or any track still waiting on a
  relink verdict.
- **Skips say why now.** A track skipped mid-run records its reason ('owned'),
  so status and plan summaries stay honest.

## 1.1.1 - never breaks in a friend's hands

A bug-fix pass on the failure modes that actually bit somebody on a fresh
machine. Nothing about a healthy run changes. A broken one just fails gracefully
now.

- **A bad config can't dump a traceback.** One stray character in config.toml
  used to spill a full Python stack across the terminal on every command. Any
  error the tool understands prints as one line now, its code and its remedy,
  and `setup` strips control characters a paste can smuggle in so the file can't
  get corrupted in the first place. Run outside a terminal, `setup` says so
  plainly instead of blaming a config file that doesn't exist yet.
- **Unreadable cookies don't fail the whole run anymore.** If the browser is
  open, or Chrome's encryption blocks access, the run says so once and continues
  without cookies instead of failing every single track with the same
  `COOKIES_UNREADABLE` line. Cookies are a boost and never a requirement.
- **Nothing floods anymore.** Any download-side failure repeating identically, a
  rate-limited IP or a broken tool or a locked file, parks the run with that
  problem's remedy after a handful of tries instead of printing the same error
  once per remaining track.
- **A gentler gear before giving up.** When YouTube starts rate-limiting mid-run
  the run downshifts to a slow one-at-a-time crawl first, trying to slip under
  the limit. Only if that can't break through does it park.
- **`relink` shows its work.** The candidate picker opens full detail on demand
  now, meaning each candidate's title, channel, length against the length we
  wanted, size, the score with its per-signal breakdown, the reason it lost, and
  the exact source URL, so you choose knowingly instead of picking a number
  blind. `c` opens the detail, a number picks straight away, `u` pastes a URL,
  and you can back out of any of them with `b` and choose another. Nothing is a
  dead end.
- **Honest resume tally.** `resume` reported "1155 ok" for a session that
  actually finished ~230 tracks, because it was counting every stage hop (fetch,
  transcode, tag, place, verify) as a success. It counts tracks that truly
  crossed the finish line now.
- **The run counter actually moves.** `run 0/281 tracks` used to sit at 0 for a
  whole run and then snap to the total at the very end, since the pipeline
  finishes all the downloads and then all the tagging, so nothing is fully done
  until the last sweep. It climbs steadily with the work now and lands on the
  true number of completed tracks. Failures fill the bar but don't pad the
  count.
- **`doctor --network`** pulls one real track from YouTube and tells you
  straight whether this machine can download *right now*. It's the fast way to
  know if it's YouTube, your cookies, or your network.

## 1.1.0 - big artists just work

All additive, and nothing from 1.0.0 changed for a normal run. Aimed squarely at
the one thing that broke, which is throwing a wall of tracks by a prolific
artist at it and having it actually finish.

- **Browser cookies.** `youtube_cookies_from_browser = "chrome"` (or firefox, or
  edge, or brave) lends downloads your browser's YouTube cookies, clearing
  age-gates and most bot walls. It's the single biggest reliability lever. The
  first-run wizard asks now, and the blocked error tells you to set it.
- **Mass-block park.** When YouTube starts bot-walling the whole run it **parks
  with the queue intact** now, saying your IP is cooling down and to resume in
  ~30 min, instead of grinding every remaining track to FAILED. A single success
  resets the detector so a healthy run never trips it. Tunable through
  `mass_block_streak`, and `0` disables it.
- **Fewer forced relinks.** Tied re-uploads that differ by a few seconds, which
  is the same audio re-encoded, auto-resolve by popularity now instead of
  stopping, so a remaster-heavy artist doesn't hand you a pile of manual picks.
  The red-flag-word guard is untouched, so a lyric or live variant still stops
  for you.
- **`song:` prefix picks the artist.** `song:Creep Radiohead` resolves to
  Radiohead now instead of the more popular Glee cover. Naming the artist in the
  query actually counts.
- **Friendlier failures.** New plain-English error codes for the things that
  actually go wrong on somebody else's machine: `NET_DNS`, `NET_CAPTIVE` for a
  wifi sign-in page, `NET_CLOCK_SKEW`, `FS_DRIVE_LOST` for an unplugged drive,
  `STORE_BUSY` and `STORE_CORRUPT`, `TOOL_QUARANTINED` for when antivirus ate
  ffmpeg, and `COOKIES_UNREADABLE`. Each one tells you what to do and `spotdlp
  explain <CODE>` spells it out.

## 1.0.0 - first stable release

The build that's actually ready to hand to somebody. Everything below works from
a double-click installer with no Python, ffmpeg, or setup on the target machine.

**The core tool**
- Point it at any Spotify artist, album, playlist, or track, or a bare name, and
  it builds an organized, fully tagged, verified library.
- Resumable to the track. A run that dies at track 812 of 4,000 keeps the 811
  and picks the rest up with `resume`.
- Explainable matching. Every candidate gets scored and persisted, and below the
  confidence floor it refuses instead of handing you the wrong recording.
- `audit` proves the library on demand: present, sound, tagged, pictured,
  accounted, and with `--identity`, acoustically confirmed against AcoustID.

**iPod and device support**
- `--to ipod` saves in AAC `.m4a`, which is what an iPod plays. `--to
  ipod-lossless` gets you Apple Lossless and `--to universal` gets you MP3. Set
  `delivery` once and it's the default, and the first-run wizard asks.
- `export --to ipod --dest <folder>` turns your existing library into an
  iPod-ready copy **offline**. It never re-downloads, never touches the archive,
  and is safe to re-run.
- iTunes-correct tags (ID3v2.3, track and disc totals), folder `cover.jpg`, and
  FAT-safe filenames for the iPod's own drive.

**Shipping**
- One `spotdlplus-setup-<version>.exe` bundles the app plus real ffmpeg,
  ffprobe, deno, and fpcalc. Verified to run with nothing else on the machine's
  PATH.
- First run opens a friendly window that connects Spotify and shows how to use
  it.
- `build.py` refuses to freeze against the wrong interpreter or a shim binary,
  so a broken build can't ship.

**Notes**
- The installer is unsigned so Windows SmartScreen warns once: More info, then
  Run anyway. Getting rid of that needs a code-signing certificate.
- Huge discographies, meaning hundreds of tracks, can trip YouTube
  rate-limiting partway. Wait, then `resume --retry-failed`.
