# spotdl+ - the contract

This isn't documentation. It's the spec that `tests/test_architecture.py`
enforces on every commit. If the code and this file disagree, the suite goes
red.

The reason is simple. A program this size stays coherent only when incoherence
is *mechanically impossible* instead of just discouraged. Discipline you have to
remember is discipline you'll forget.

---

## 1. Layers

Imports flow downward, always. A layer can import from its own layer and from
anything beneath it. It can never import from a layer above it.

| # | Layer        | Owns                                                      | May import        |
|---|--------------|-----------------------------------------------------------|-------------------|
| 0 | `core/`      | errors, events, models, store, config, engine, backoff, works | *nothing internal* |
| 1 | `net/`       | http client, auth, rate limit, retry, circuit breaker      | core              |
| 2 | `providers/` | spotify, musicbrainz, acoustid                             | core, net         |
| 3 | `match/`     | candidate search, scoring, the matcher                     | core, net         |
| 4 | `media/`     | fetch (yt-dlp), transcode (ffmpeg), tag (mutagen), lyrics, place | core, net   |
| 5 | `pipeline/`  | resolver, expander, dedupe, planner, stages, orchestration | 0-4               |
| 6 | `cli/`       | commands, renderer, doctor                                 | anything          |

`core/` is the keystone. It imports the Python standard library and nothing
else, so it has no supply chain and it tests in under a second. Every layer
above depends on it, which makes it the one place where a mistake is genuinely
expensive.

## 2. The seam

**The core doesn't print.** Not for debugging. Not just this once.

Nothing outside `cli/` may `print()` or import `rich`, `typer`, `click`,
`argparse`, or `tqdm`. Anything that wants to say something emits a typed event
into the `EventBus` and forgets about it.

This one decision is what makes a GUI possible. The CLI is one subscriber, a GUI
is another, and a JSONL audit log is a third. None of them can see each other
and the core can't see any of them. The moment one function in the pipeline
calls `print()`, status stops being an object and becomes text on its way to a
terminal, and every non-terminal consumer is dead. spotdl made that mistake in
its first hundred lines and never got it back.

## 3. Units

- **Durations are integer milliseconds.** Never float seconds.
- **Sizes are integer bytes.** Never megabytes, never floats.
- **Scores are floats in `[0.0, 1.0]`.** The only place floats are legal.

A duration of `3.0000000004` minutes is how a dedupe pass fails to notice two
identical recordings.

## 4. Identity

A track's primary key is its **ISRC**, because an ISRC names a *recording* and stays
identical across every release that recording appears on. The album, the deluxe
reissue, the Japan press, the greatest-hits compilation, all the same.

Identity falls back in strict order:

    isrc:<ISRC>  ->  mbid:<recording-id>  ->  sp:<spotify-id>  ->  fuzzy:<artist:title:duration-bucket>

### Tier 1, the same recording

`UNIQUE(run_id, identity)` in the schema. The database physically can't hold the
duplicate, so any code that checks for duplicates before inserting is redundant.

This catches the common case and it's verified against the live API. `Kid A` and
the `KID A MNESIA` compilation share all 11 ISRCs, because a compilation reuses the
original masters.

### Tier 2, the same *work* on a different master

Tier 1 is necessary and **not sufficient**, and pretending otherwise was wrong.
Measured against the live API:

    OK Computer                    Airbag               GBAYE9701274   287.9s
    OK Computer OKNOTOK 1997 2017  Airbag - Remastered  GBBKS1700107   283.8s

A remaster is a new master so it earns a new ISRC. ISRC alone would hand you 35
tracks where 23 exist. Two recordings are the same *work* when:

    same primary artist  AND  same normalized title  AND  |Δduration| <= 8s

A few seconds of tolerance, because remasters routinely drift a little on trim and
fade while a radio edit or a live take drifts a lot more. The negative control
holds. `In Rainbows` and `In Rainbows (Disk 2)` share no ISRCs and no normalized
titles and must never merge.

Tier 2 can't be a database constraint, because collapsing two masters needs a
*policy*, meaning which one do you keep. So it's a real pass making a real
decision, governed by `SelectionProfile.master_preference`. Tier 1 is physics
and tier 2 is taste.

Measured end to end on Radiohead under `canonical`:

    expander produced         217 tracks
    tier 1 (ISRC constraint)   21 collapsed  -> 196 identities
    tier 2 (work + master)     18 collapsed  -> 178 queued

The pass compares every recording of a song across *all* releases at once
instead of album against album. 6 of those 18 are OKNOTOK's remastered B-sides
folding into their originals on 1997-era singles.

Losers get marked `SKIPPED` with `skip_reason='duplicate:work'` and never
deleted. Nothing vanishes silently from an archive, and `explain` can always say
which master replaced which. Nothing, ever.

## 5. The store is the queue

Work lives in SQLite and never in a Python list. A run over 4,000 tracks costs
the same memory as a run over one.

Workers **lease** tracks instead of owning them. A worker that dies holding a
lease loses it when the lease expires and the track goes back to the pool.
Nothing ever has to be unstuck by hand.

Every track walks the state machine in `models.TRANSITIONS` exactly once.
Illegal transitions raise `StateTransitionError`, loudly, because an illegal
transition is always a bug and never user input. A half-fetched file must not be
able to reach the tagging stage.

## 6. Every stage is the same shape

A pipeline stage declares what state it consumes, what state it produces, and a
`run()` that does one thing. It doesn't handle retries, doesn't emit progress
events, doesn't touch the state machine, and doesn't sleep.

```python
class Fetch:
    name     = Stage.FETCH
    consumes = TrackState.MATCHED
    produces = TrackState.FETCHED

    def run(self, ctx, row) -> dict[str, Any]:
        ...                 # do the work, or raise a SpotdlPlusError
        return {'est_bytes': n}
```

The **Engine** does everything else. It claims the batch, bounds the
concurrency, advances the state, applies the retry policy the error carried,
emits the events, and parks the run when the network dies.

This is the coherence keystone. Retry logic lives in exactly one place so it
can't drift between stages. A new stage gets resumability, backoff, rate-limit
obedience, and event emission for free, and more to the point it **can't get
them wrong**, because it never sees them.

### 6a. Who's allowed to wait

Nothing in `providers/`, `match/`, `media/`, or `pipeline/` may block. No
`sleep()`, no `backoff_delay()`. If it needs to wait it raises and the Engine
decides.

`net/` is infrastructure rather than a stage and it's the one exception, but a
bounded one. It may block inline for **at most `ratelimit.MAX_BLOCK_S` (2
seconds)** to pace requests, using a condition variable instead of a bare sleep
so a shutdown can interrupt it. Any wait longer than that has to be raised as
`RateLimited(retry_after=…)` so the Engine can defer the track and free up the
thread.

The rule in one line: cheap waits get paced inline, expensive waits get
deferred. A worker sitting out a 60-second rate limit is a worker doing nothing.

## 7. Failure is typed

Nothing raises a bare `Exception`. Every failure is a `SpotdlPlusError` subclass
carrying four things:

- a stable **code** (`RATE_LIMITED`, `AUTH_REFRESH_LOOP`, `MATCH_NONE`, …)
- a **retry policy** (`NEVER` / `NOW` / `BACKOFF` / `AFTER` / `PARK`)
- a **remedy**, meaning a sentence telling a human what to do
- structured **context**

The retry policy is data, not code, and the Engine reads it. A stage that raises
`RateLimited(retry_after=4.5)` gets a 4.5-second wait and a retry without ever
knowing retries exist.

`PARK` isn't failure. `NET_OFFLINE` and `FS_NO_SPACE` freeze the run with its
queue intact and its finished tracks on disk, and `spotdl+ resume` picks up
where it stopped.

## 8. Matching is explainable

Every candidate we consider gets persisted, the winner and every loser with the
reason it lost. A wrong match stops being a mystery the moment you can read the
scoreboard, and `relink` stops being a rebuild the moment the scoreboard is on
disk.

Below the confidence floor (`MatchResult.CONFIDENCE_FLOOR`) we **skip the track
instead of guessing**. A missing song is a nuisance you can see. A wrong song
that looks right corrupts an archive quietly.

Duration anchors the score at half its weight, because it's the signal a faker can't
fake. A live take is genuinely longer and a sped-up edit genuinely shorter.
Title and channel refine it, view count only breaks ties, and negative keywords
(`live`, `karaoke`, `nightcore`, `sped up`, …) apply as a *multiplier* judged
against the target, so a word only counts as a penalty when the candidate has it
and the track you asked for doesn't.

Two refusals, both `NEVER`-retry since searching again finds the same
candidates. `MATCH_NONE` when nothing clears the floor, and `MATCH_AMBIGUOUS`
when the top two clear it, score within a hair, **and have different
durations**. That last clause got learned live on 'bad guy'. Three lyric and
audio re-uploads tied at 0.87, all of them the correct 195s album track, while
the official video sat lower with its 206s extended-outro cut. A tie between
interchangeable copies of the *same* recording isn't an ambiguity and view count
resolves it deterministically. Only a tie between plausibly *different* takes
stops for a human.

## 9. Nothing half-written reaches the library

Downloads land in a cache directory. Transcodes land in the cache directory. The
final move into your library is an atomic rename after an fsync. There's no
window anywhere where a partial file looks finished.

### Known sharp edge: cross-run placement (found by `audit`, 2026-07-15)

When the same recording exists in two runs, run B's placement can overwrite a
file run A already verified. If B's copy then fails verification, the library
still vouches for A's good bytes, which don't exist anymore. The first
full-library audit found 13 of these among 1,202 files.

What's in place now: `spotdl+ audit` detects the state, meaning library-owned
but unreadable, and `force_relink` repairs it, and the fetch stage probes
downloads at the door so a corrupt B-copy is much less likely to reach placement
at all. The *principled* fix is reordering the walk so verification happens on
the cache artifact BEFORE placement, which would mean the library folder only
ever receives already-verified bytes. That's a state-machine change (TAGGED ->
VERIFIED -> PLACED/DONE) and it's deferred until the current shape has soaked
longer. Take it when you're touching the state machine for some other reason
anyway.

## 10. Build order

Each phase ends with the full suite green, this contract included.

| Phase | Deliverable                                        | Status |
|-------|----------------------------------------------------|--------|
| 0     | spine: errors, events, models, store               | done   |
| 1     | config + engine + backoff (the shape of a stage)   | done   |
| 2     | net: auth, single-flight, breaker, bucket, http, TLS | done |
| 3     | providers: spotify, musicbrainz (deezer: cut)      | done   |
| 4     | resolver + expander (any input -> entity tree)     | done   |
| 5     | dedupe pass into the store + selection profiles    | done   |
| 6     | matcher (duration-anchored, scored, explainable)   | done   |
| 7     | media: fetch, transcode, tag, place, verify        | done   |
| 8     | planner + size preview + free-space gate           | done   |
| 9     | cli (get/plan/resume/status/doctor/explain)        | done   |
| 10    | hardening: the full corpus into a real library, soak, docs | done |

Phase 10's soak was the corpus itself. 1,202 verified recordings across 56
artists in ~13 hours of sustained operation, surviving 2 process kills, a
MusicBrainz 429, and ~20 corrupted streams, every one caught, typed, and
recovered.

### What phase 7 taught me, live (2026-07-14)

Downloads stalled with 'read timed out' on every YouTube client, token or not.
The cause was none of the suspects. **The machine's IPv6 route blackholes
data**, so the SYN succeeds and the bytes never come, and googlevideo's media
edges prefer v6 while youtube.com's API happened to work anyway.
`source_address: 0.0.0.0`, which forces IPv4, fixed it instantly at 22 MiB/s.
The full yt-dlp environment, meaning IPv4, the OS trust store, a
self-provisioned deno.exe, and the bgutil PO-token script, lives in
`net/ytenv.py`. `doctor` checks each layer separately, because when YouTube breaks
again the first question is always which layer.

Also learned: one `YoutubeDL` per worker *thread*, reused across tracks. yt-dlp
caches solved JS challenges and PO tokens per instance, cold instances spawn
deno per download, and two cold starts colliding is how 6 album tracks died to
TimeoutExpired while 7 made it.

### What the first sustained run taught me (94-track catalog, 2026-07-14)

Under sustained downloading YouTube sometimes truncates or corrupts a stream
mid-transfer and yt-dlp reports success anyway. **The verify gate caught all 20
corrupt files**, meaning CRC-mismatched ogg pages and one file running 0s
against a 152s expectation, and refused to record any of them as owned. Two
hardening changes came out of it.

- **Integrity at the door.** The fetch stage ffprobes every download now, about
  50ms, and raises a *retryable* error on corruption, because at fetch a retry means
  a refetch. Five stages later at verify it's a dead end.
- **Fresh work always wins at placement.** PlaceStage used to skip when the
  destination existed, which permanently wedged a corrupt file, since every
  retry fetched a good copy and then declined to use it. The exists-shortcut now
  only applies when there's nothing in the cache to place.

Separately, a real MusicBrainz 429 escaped expansion, which runs OUTSIDE the
Engine where nobody reads retry policy, and killed two artist runs. Enrichment
treats throttling as degraded classification now, so it warns and keeps
Spotify's guess, and the MusicBrainz budget dropped to 0.9/s. Pacing at exactly
their 1/s limit means any jitter puts you over, and they 429'd a run that did
precisely that.

## 11. Out of scope, deliberately

**Deezer.** Planned, then cut on evidence. Its only job was backfilling ISRCs
Spotify withholds. Measured across 269 tracks from 8 deliberately awkward
artists, being Glenn Gould, Fishmans, Duster, The Caretaker, Aphex Twin, MF
DOOM, Godspeed You! Black Emperor, and Taylor Swift, Spotify returned an ISRC
for **269 of 269**. Modern distributors assign them universally.

A fourth provider means a fourth rate-limit budget, a fourth set of failure
modes, and a fourth thing to keep in tune, in exchange for solving a problem
we've seen zero times. The identity fallback chain in §4 already covers it if it
ever shows up. Reinstate this when a real run produces a track with no ISRC and
not before.

**Stream ripping from Spotify.** That's DRM circumvention, so it isn't a feature
and it never will be. Audio comes from YouTube through yt-dlp.
