# Security

## Reporting something

Open a [private security advisory](https://github.com/Golden-Atlas/spotdl-plus/security/advisories/new)
instead of a public issue. I'll acknowledge inside a week. This is a personal
project and not a company, so that's a promise about attention, not a support
SLA.

Worth reporting: anything that could expose somebody's credentials, write
outside the library folder, execute code out of a downloaded file, or turn a
malicious search result into something worse than a bad song.

## What spotdl+ does with your secrets

- Your Spotify credentials are **yours**. You create the app, you paste the
  keys, and they live in `%LOCALAPPDATA%\spotdlplus\config\config.toml` on your
  machine and nowhere else. spotdl+ ships no credentials and phones nothing
  home.
- `doctor` prints your resolved config with every secret shown as `<set>`, on
  purpose, because `doctor` output is the thing people paste into issues.
- `report` is built for pasting: versions, counts, error codes, and context. It
  leaves your config out. Skim it anyway, since file paths in error context can
  carry your username, and that call is yours to make and not mine.
- Nothing gets written outside your library folder and the app's own config,
  cache, and state directories. Rendered paths are sanitized and then re-checked
  to confirm they can't escape the library root.

## The parts that touch the outside world

- **Downloads run through yt-dlp** in a subprocess. It's the biggest attack
  surface here and keeping it current is the best single thing you can do.
  `doctor` warns when it's too old to trust.
- **Bundled binaries** (ffmpeg, ffprobe, deno, fpcalc) are unmodified upstream
  builds, resolved by absolute path out of the app's own folder and never from
  PATH, so a stray `ffmpeg.exe` in a downloads folder can't get picked up
  instead. If one disappears mid-run we say antivirus ate it instead of quietly
  continuing.
- **Browser cookies**, if you turn them on, get read by yt-dlp from your local
  browser profile and used only for YouTube requests. If they can't be read we
  warn once and run without them. They never get copied anywhere.
- **TLS** goes through your OS certificate store, which is what lets this work
  behind corporate proxies and antivirus interception instead of failing for no
  visible reason.

## Not a vulnerability

- The installer is unsigned so SmartScreen warns on first run. That's a missing
  code-signing certificate (~$400/yr) and not a flaw. Build from source if it
  bothers you, which is fair.
- yt-dlp getting blocked by YouTube. Annoying, not a security issue.
