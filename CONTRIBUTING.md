# Contributing

Thanks for looking. spotdl+ is a small opinionated codebase and the opinions are
the point, since most of them got paid for with a bug. This file is the short
version of what they are, so a patch doesn't have to find them the hard way.

## Getting it running

```bash
git clone https://github.com/Golden-Atlas/spotdl-plus
cd spotdl-plus
python -m venv venv                 # 3.11 or newer
venv/Scripts/python -m pip install -e ".[dev]"
venv/Scripts/python -m pytest       # ~470 tests, all offline, ~30s
```

That install is everything you need to develop. You do **not** need ffmpeg,
deno, or an installer toolchain to work on the code or run the tests. Those are
only for building the shipped .exe, which is at the bottom.

To actually use it you'll want your own free Spotify app credentials, and
`spotdlp setup` walks you through it. Nobody's credentials live in this repo and
none ever should, including in a test fixture. That's a mistake this project has
already made once.

## The rules that aren't negotiable

`tests/test_architecture.py` enforces these and fails the build if you break
one. They read like pedantry and work like guardrails.

1. **The core never prints.** `core/` emits typed events into a bus and the CLI
   subscribes and renders. That seam is the whole reason a GUI is possible.
2. **The layers point one way.** core, net, providers, match, media, pipeline,
   cli. Nothing imports upward, ever.
3. **`core/` is stdlib-only.** No third-party imports below the net layer.
4. **Every failure is a typed error with a remedy.** If a person can see it, it
   carries a code, a retry policy, and a sentence telling them what to do.
   `raise ValueError('nope')` is how the tool loses somebody's trust at 3am.
5. **A stage never retries.** Stages do one thing and raise. The Engine owns
   every retry, backoff, and park decision in exactly one place.

## Style

ruff enforces the code style (`venv/Scripts/python -m ruff check src`). Single
quotes, and the config settles anything else.

Prose has a house style too and it's worth matching, so the tool sounds like one
program instead of a committee. Three registers:

- **Comments and docstrings.** A journal. First person, past tense for the war
  stories and present for behavior. Shorthand is fine here. This is you talking
  to whoever reads it in two years, which is usually you.
- **Anything the tool prints.** Remedies, help text, status lines. Direct talk,
  no shorthand, no cleverness that costs clarity. Somebody reads this at 3am
  with a broken run and it has to land on the first pass.
- **Docs.** Story first, spec second. Say why the thing exists before you say
  what it does.

Across all three: plain declaratives doing one thing each. Open with a verb that
says what the code does, not with a framing sentence about what this section
covers. State the fact, then your honest read on it. Keep the receipts, meaning
real numbers and real counts and what actually happened, so "caught 4 wrong
labels" instead of "improved accuracy". An aside goes in parentheses. Exact
numbers when they matter and `~`-hedged when they don't.

What to avoid is polish for its own sake. Aphorisms stacked three deep read like
a performance for a reviewer instead of a person explaining their program. If a
sentence sounds like a tagline, cut it. Same for the closing line that restates
what you just said in a prettier way.

## Tests

Every behavior change wants a test and the good ones read like a claim:
`test_a_wall_of_blocks_parks_the_run_not_every_track`. Tests have to be offline
and hermetic, so no network, no personal paths, and no writing outside
`tmp_path`. The suite has to pass on a stranger's laptop with no config and no
library.

If you're fixing a bug, write the failing test first if you can and keep the war
story in a comment. Half this codebase's comments explain a scar and they're the
reason the same bug hasn't come back twice.

## Building the installer (optional)

Only needed if you're shipping a Windows .exe:

```bash
venv/Scripts/python build.py
```

It wants [Inno Setup 6+](https://jrsoftware.org/isinfo.php) on PATH and it
harvests ffmpeg, ffprobe, deno, and fpcalc into `vendor/` on first run. It
refuses to build against the wrong interpreter or a shim binary so a broken
installer can't ship by accident. That guard exists because one did.

One trap worth knowing: `build.py` sits in the repo root, so `python -m build`
run from here imports THAT instead of the PyPA `build` package and you get the
installer script. If you want a wheel or an sdist, run it from outside the repo:
`python -m build path/to/spotdl-plus`.

## Pull requests

- One idea per PR. A 200-line diff that does two things is two PRs.
- `pytest` green and `ruff check src` clean before you open it.
- Say what broke and how you know it's fixed. Paste the output.
- If you changed anything a person sees, paste that too. Terminal screenshots
  are fine and honestly preferred.

## What I'll probably say no to

Not to be discouraging, just so you don't spend a weekend on it.

- **Anything that guesses.** The confidence floor refuses instead of handing you
  the wrong recording. "Just pick the top result" is the bug this tool exists to
  not have.
- **Silent degradation.** If art or lyrics or a tag can't be had, we say so and
  move on. Quiet fallbacks are how a library rots without anybody noticing.
- **A second place that knows about retries.** See rule 5.
- **New dependencies without a real argument.** Every one of them is a thing
  that can break somebody's install two years from now.

## Licensing your contribution

spotdl+ is GPL-3.0-or-later, and
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) explains why that's arithmetic
and not preference. Opening a PR means you agree your contribution ships under
the same terms.
