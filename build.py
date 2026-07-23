'''
build.py - packages spotdl+ into a Windows installer a friend can double-click.

Run it and it does the whole job:

    1. reads the version straight out of the source, so this file never drifts
    2. harvests the helper binaries (ffmpeg, ffprobe, deno, fpcalc) from wherever
       they already live on this machine into vendor/, taking what it sees
    3. freezes the app with PyInstaller: ONLY what the program needs to run, no
       docs, no tests, no markdown
    4. drops the binaries next to the frozen .exe so it works with or without PATH
    5. wraps the whole thing in an Inno Setup installer with an 'Add to PATH'
       checkbox and in-place upgrades, or a portable zip if Inno isn't installed

Send the resulting dist/spotdlplus-setup-<version>.exe to anyone. When you fix a
bug later, run this again and send the new one. It upgrades in place and leaves
their config and library untouched.

    python build.py                 # full build: freeze + installer
    python build.py --portable      # skip the installer, just make the zip
    python build.py --no-clean      # reuse the last freeze (faster iteration)

Prereqs on THIS machine, one time: `pip install -e .[dev]`, which brings
PyInstaller, and for the installer step, Inno Setup 6
(https://jrsoftware.org/isdl.php). Without Inno you still get a working
portable zip.
'''

from __future__ import annotations

import argparse
import atexit
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / 'src'
VENDOR = ROOT / 'vendor'
BUILD = ROOT / 'build'
DIST = ROOT / 'dist'
#: Every shipped build is copied here under its version, so you always have the
#: exact bytes you sent, at releases/<version>/spotdlplus-setup-<version>.exe.
RELEASES = ROOT / 'releases'

APP_NAME = 'spotdl+'          # what people call it
EXE_NAME = 'spotdlp'         # the command they type / the .exe filename
PUBLISHER = 'Kai Watts'
#: Stable across every version ON PURPOSE, because it's how the installer knows to
#: upgrade in place instead of stacking a second copy. Never change it.
APP_ID = '{{9A5F2C1E-7B3D-4E88-9F1A-2C6D8B0E4A17}}'

#: name -> (required?, where to get it if it's missing). Required ones are needed
#: to download at all. Fpcalc only powers `audit --deep/--identity`.
BINARIES = {
    'ffmpeg':  (True,  'https://www.gyan.dev/ffmpeg/builds/  (ffmpeg-release-essentials.zip → bin/)'),
    'ffprobe': (True,  'ships inside the same ffmpeg zip as ffmpeg'),
    'deno':    (True,  'https://github.com/denoland/deno/releases  (deno-x86_64-pc-windows-msvc.zip)'),
    'fpcalc':  (False, 'https://acoustid.org/chromaprint  (chromaprint-fpcalc-*-windows-x86_64.zip)'),
}


# ---------------------------------------------------------------------------
# little helpers
# ---------------------------------------------------------------------------

def acquire_lock() -> None:
    '''
    Refuses to run two builds at once. They share dist/spotdlp and PyInstaller
    wipes it then repopulates it, so a second run mid-flight deletes the first
    run's exe out from under it and leaves a half-empty folder. Learned that the
    hard way. O_EXCL makes the claim atomic, so exactly one build wins.
    '''
    BUILD.mkdir(exist_ok=True)
    lock = BUILD / '.build.lock'
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        sys.exit(f'another build looks like it\'s already running '
                 f'(lock: {lock}).\nIf you\'re sure it isn\'t, delete that file '
                 f'and try again.')
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    atexit.register(lambda: lock.unlink(missing_ok=True))


def say(msg: str) -> None:
    print(f'  {msg}')


def section(title: str) -> None:
    print(f'\n=== {title} ===')


def read_version() -> str:
    '''Straight from the source of truth, so the installer name never lies.'''
    text = (SRC / 'spotdlplus' / '__init__.py').read_text(encoding='utf-8')
    m = re.search(r"__version__\s*=\s*['\"]([^'\"]+)['\"]", text)
    if not m:
        sys.exit('could not find __version__ in src/spotdlplus/__init__.py')
    return m.group(1)


def _tools_dir() -> Path:
    '''Where the app self-provisions deno and fpcalc. Same path net/ytenv.py uses.'''
    root = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~\\AppData\\Local')
    return Path(root) / 'spotdlplus' / 'tools'


# ---------------------------------------------------------------------------
# stage 1: find the binaries
# ---------------------------------------------------------------------------

#: How to make each binary prove itself, and a word its real output must contain.
#: A Chocolatey/scoop SHIM run from a new folder prints a 'cannot find file'
#: error and even exits 0, so returncode alone is a liar. We check the text.
_VERSION_PROBE = {
    'ffmpeg':  (['-version'], 'ffmpeg version'),
    'ffprobe': (['-version'], 'ffprobe version'),
    'deno':    (['--version'], 'deno'),
    'fpcalc':  (['-version'], 'fpcalc'),
}


def _runs_ok(name: str, exe: Path) -> bool:
    '''Does this exact file actually work where it sits? (Shims fail here.)'''
    args, needle = _VERSION_PROBE[name]
    try:
        r = subprocess.run([str(exe), *args], capture_output=True, timeout=20)
    except Exception:  # noqa: BLE001  # a binary that won't even launch is a no
        return False
    out = (r.stdout + b' ' + r.stderr).decode('utf-8', 'replace').lower()
    return needle.lower() in out


def _resolve_shim(name: str, src: Path) -> Path | None:
    '''
    A Chocolatey shim under chocolatey\\bin is a tiny stub that finds the real
    binary RELATIVE TO ITSELF, so copying it elsewhere breaks it. That's how a
    whole Smashing Pumpkins discography failed 284/284. Digs out the real binary
    under chocolatey\\lib, meaning the big file and not another stub.
    '''
    parts = [p.lower() for p in src.parts]
    if 'chocolatey' not in parts:
        return None
    choco = Path(*src.parts[:parts.index('chocolatey') + 1])
    cands = [c for c in (choco / 'lib').glob(f'**/{name}.exe')
             if c.stat().st_size > 1_000_000]   # real ffmpeg is tens of MB
    return max(cands, key=lambda c: c.stat().st_size) if cands else None


def harvest_binaries() -> tuple[dict[str, Path], list[str]]:
    '''
    Collect every helper binary this machine has into vendor/, and prove each one
    RUNS from there before trusting it. Returns (found, missing_required).
    Adaptable on purpose: it takes what it sees, resolves shims, and tells you
    exactly what it couldn't make work.
    '''
    section('binaries')
    VENDOR.mkdir(exist_ok=True)
    found: dict[str, Path] = {}
    missing_required: list[str] = []

    for name, (required, where) in BINARIES.items():
        vexe = VENDOR / f'{name}.exe'

        # A cached vendor copy is only good if it still actually works.
        if vexe.is_file() and _runs_ok(name, vexe):
            found[name] = vexe
            say(f'[have]   {name}.exe  (vendor/, verified)')
            continue

        which = shutil.which(name)
        which_path = Path(which) if which else None
        # Try the resolved real binary FIRST, then the raw which() (which may be
        # a shim), then our tools dir. First one that verifies wins.
        sources = [
            _resolve_shim(name, which_path) if which_path else None,
            which_path,
            _tools_dir() / f'{name}.exe',
        ]

        picked: Path | None = None
        for src in sources:
            if src is None or not Path(src).is_file():
                continue
            shutil.copy2(src, vexe)
            if _runs_ok(name, vexe):
                picked = Path(src)
                break

        if picked is not None:
            found[name] = vexe
            say(f'[copied] {name}.exe  <- {picked}  (verified)')
        else:
            vexe.unlink(missing_ok=True)   # don't leave a broken copy behind
            tag = 'MISSING' if required else 'skipped'
            say(f'[{tag}] {name}.exe  none that runs. get a real one: {where}')
            if required:
                missing_required.append(name)

    return found, missing_required


# ---------------------------------------------------------------------------
# stage 2: freeze
# ---------------------------------------------------------------------------

def ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        sys.exit('PyInstaller is not installed. Run:  pip install -e .[dev]')


#: The app's direct third-party imports, by the name you'd `import`. If one can't
#: be imported by the python doing the freeze, PyInstaller can't bundle it and the
#: exe crashes on first run (how we shipped `No module named 'typer'`). Import
#: names, not pip names (yt-dlp -> yt_dlp). typer's own chain (its vendored click,
#: shellingham, rich, annotated_doc, colorama) rides along with `import typer`
#: succeeding, so checking typer covers them.
RUNTIME_IMPORTS = [
    'httpx', 'truststore', 'yt_dlp', 'mutagen', 'rapidfuzz', 'rich', 'typer',
]


def preflight_deps() -> None:
    '''
    Prove every runtime module imports in THIS python before we freeze. Freezing
    against a python that's missing one just moves the ImportError from now (loud,
    fixable) to your friend's first run (silent, shipped). Fail here instead.
    '''
    section('preflight')
    missing = [m for m in RUNTIME_IMPORTS
               if subprocess.run([sys.executable, '-c', f'import {m}'],
                                 capture_output=True).returncode != 0]
    if missing:
        msg = (
            f'these runtime modules can\'t be imported by the python that\'s '
            f'building ({sys.executable}):\n'
            f'  {", ".join(missing)}\n\n'
            f'Freezing now would ship an exe that crashes on startup.\n'
        )
        # The usual cause: an IDE ran this with some OTHER project's interpreter.
        # If this project's own venv is sitting right here, just point at it.
        ours = ROOT / 'venv' / 'Scripts' / 'python.exe'
        if ours.is_file() and Path(sys.executable).resolve() != ours.resolve():
            msg += (
                f'\nThis is usually the wrong interpreter (an IDE default). '
                f'This project has its own venv. Build with that instead:\n'
                f'  "{ours}" build.py\n'
            )
        else:
            msg += (
                f'\nInstall the project into THIS python first, then rebuild:\n'
                f'  "{sys.executable}" -m pip install -e .[dev]\n'
            )
        sys.exit(msg)
    say(f'all {len(RUNTIME_IMPORTS)} runtime modules import cleanly')


def write_entry() -> Path:
    '''The one-line launcher PyInstaller freezes. Generated, never hand-edited.'''
    BUILD.mkdir(exist_ok=True)
    entry = BUILD / '_entry.py'
    entry.write_text(
        'from spotdlplus.cli.main import main\n'
        "if __name__ == '__main__':\n"
        '    main()\n',
        encoding='utf-8',
    )
    return entry


def freeze(entry: Path, clean: bool) -> Path:
    '''Run PyInstaller. Returns the folder holding the frozen exe.'''
    section('freeze')
    out = DIST / EXE_NAME
    if not clean and (out / f'{EXE_NAME}.exe').is_file():
        say('reusing the last freeze (--no-clean)')
        return out

    argv = [
        sys.executable, '-m', 'PyInstaller',
        '--noconfirm', '--onedir', '--console',
        '--name', EXE_NAME,
        '--distpath', str(DIST),
        '--workpath', str(BUILD / 'pyi'),
        '--specpath', str(BUILD),
        '--paths', str(SRC),
        # yt-dlp lazy-imports a zoo of extractors. pull them all or YouTube breaks.
        '--collect-all', 'yt_dlp',
        # The command surface and its renderer. Since typer 0.26 vendors click
        # inside itself, its new package layout is exactly what PyInstaller's
        # import scan failed to bundle (the `No module named 'typer'` we shipped).
        # collect-all pulls typer whole, vendored click and all, plus rich's
        # data files and shellingham's shell probes.
        '--collect-all', 'typer',
        '--collect-all', 'rich',
        '--collect-all', 'shellingham',
        # dead weight we never call. Keep the bundle to only what runs.
        '--exclude-module', 'pytest',
        '--exclude-module', '_pytest',
        '--exclude-module', 'tkinter',
        '--exclude-module', 'test',
    ]
    # A real icon if the project ships one. The exe, its shortcuts, and the
    # installer all inherit it. Optional: drop a spotdlp.ico in assets/ to use it.
    icon = ROOT / 'assets' / 'spotdlp.ico'
    if icon.is_file():
        argv += ['--icon', str(icon)]
        say(f'using icon {icon.name}')
    if clean:
        argv.append('--clean')
    argv.append(str(entry))

    say('running PyInstaller (this takes a minute)...')
    subprocess.run(argv, check=True)
    return out


def stage_binaries(app_dir: Path, found: dict[str, Path]) -> None:
    '''Drop the helper binaries next to the frozen exe so it's self-contained.'''
    section('staging binaries next to the exe')
    for name, path in found.items():
        dst = app_dir / f'{name}.exe'
        shutil.copy2(path, dst)
        say(f'+ {dst.name}')
    if not found:
        say('(none staged, so the package leans on the user\'s PATH)')


def smoke_test(app_dir: Path) -> None:
    '''
    Actually RUN the frozen exe before we let it near an installer. A build that
    hands a friend an .exe it never executed itself is how a missing module ships.
    Two cheap invocations exercise the whole import chain (typer, rich, click, the
    app package) without needing config or a network: `version` and `-h`. If either
    crashes or a traceback leaks we stop right here. No installer, no zip.
    '''
    section('smoke test (running the frozen exe)')
    exe = app_dir / f'{EXE_NAME}.exe'
    if not exe.is_file():
        sys.exit(f'freeze produced no {exe.name}, so nothing to test.')
    checks = [
        (['version'], __import__('re').escape(read_version())),  # prints "spotdlp X.Y.Z"
        (['-h'], 'get'),                                          # the help lists commands
    ]
    for args, must_contain in checks:
        try:
            # Decode as UTF-8, not the Windows locale codec: rich prints box-drawing
            # characters, and text=True would blow up decoding them as cp1252.
            r = subprocess.run([str(exe), *args], capture_output=True,
                               encoding='utf-8', errors='replace', timeout=90)
        except subprocess.TimeoutExpired:
            sys.exit(f'the frozen exe hung on `{EXE_NAME} {" ".join(args)}`. not shipping it.')
        blob = (r.stdout or '') + (r.stderr or '')
        crashed = (r.returncode != 0 or 'Traceback' in blob
                   or 'ModuleNotFoundError' in blob or 'Failed to execute script' in blob)
        if crashed:
            print(blob[-1800:])
            sys.exit(
                f'\nthe frozen exe CRASHED on `{EXE_NAME} {" ".join(args)}`. NOT '
                f'building an installer around a broken exe.\nUsually a missing '
                f'--collect-all for whatever module the traceback names.')
        import re as _re
        if must_contain and not _re.search(must_contain, blob):
            sys.exit(f'`{EXE_NAME} {" ".join(args)}` ran but its output looked wrong '
                     f'(expected to see {must_contain!r}).')
        say(f'[ok] {EXE_NAME} {" ".join(args)}')
    say('the frozen exe runs. safe to package.')


def write_launcher(app_dir: Path) -> None:
    '''
    The window the installer opens when it finishes, and the Start Menu shortcut
    later. It runs the friendly `welcome` screen, then drops the person into a
    live prompt in the app folder, so `spotdlp get ...` just works even if they
    never added anything to PATH. CRLF endings because it's a Windows batch file.
    '''
    section('launcher')
    bat = app_dir / 'Start.bat'
    lines = [
        '@echo off',
        'cd /d "%~dp0"',
        'title spotdl+',
        '"%~dp0spotdlp.exe" welcome',
        'echo.',
        'cmd /k',
        '',
    ]
    bat.write_text('\r\n'.join(lines), encoding='utf-8', newline='')
    say(f'+ {bat.name}')


# ---------------------------------------------------------------------------
# stage 3: installer (or portable zip)
# ---------------------------------------------------------------------------

def find_iscc() -> Path | None:
    '''The Inno Setup compiler, wherever any version of it landed.'''
    hit = shutil.which('ISCC')
    if hit:
        return Path(hit)
    bases = [
        os.environ.get('ProgramFiles'),
        os.environ.get('ProgramFiles(x86)'),
        os.environ.get('ProgramW6432'),
        str(Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs') if os.environ.get('LOCALAPPDATA') else None,
    ]
    hits: list[Path] = []
    for base in bases:
        if not base:
            continue
        # 'Inno Setup 6', 'Inno Setup 7', and so on. Take whatever's there.
        hits += Path(base).glob('Inno Setup*/ISCC.exe')
    # Newest name sorts last, so prefer it.
    return sorted(hits)[-1] if hits else None


def write_iss(version: str, app_dir: Path) -> Path:
    '''Generate the Inno Setup script fresh each build, so version can't drift.'''
    BUILD.mkdir(exist_ok=True)
    iss = BUILD / 'installer.iss'
    icon = ROOT / 'assets' / 'spotdlp.ico'
    filled = (_ISS_TEMPLATE
              .replace('@APP_NAME@', APP_NAME)
              .replace('@EXE_NAME@', EXE_NAME)
              .replace('@VERSION@', version)
              .replace('@PUBLISHER@', PUBLISHER)
              .replace('@APP_ID@', APP_ID)
              .replace('@ICON@', str(icon))
              .replace('@SRC_DIR@', str(app_dir))
              .replace('@LICENSE@', str(ROOT / 'LICENSE'))
              .replace('@NOTICES@', str(ROOT / 'THIRD-PARTY-NOTICES.md'))
              .replace('@OUT_DIR@', str(DIST)))
    # Inno refuses to compile if SetupIconFile points at a file that isn't there,
    # so only keep that line when we actually have an icon to point it at.
    if not icon.is_file():
        filled = '\n'.join(ln for ln in filled.splitlines()
                           if not ln.strip().startswith('SetupIconFile='))
    iss.write_text(filled, encoding='utf-8')
    return iss


def build_installer(version: str, app_dir: Path) -> Path | None:
    section('installer')
    iscc = find_iscc()
    if iscc is None:
        say('Inno Setup not found, so skipping the installer.')
        say('Install it once from https://jrsoftware.org/isdl.php to get a setup.exe.')
        return None
    iss = write_iss(version, app_dir)
    say(f'compiling with {iscc}...')
    subprocess.run([str(iscc), str(iss)], check=True)
    return DIST / f'spotdlplus-setup-{version}.exe'


def make_portable_zip(version: str, app_dir: Path) -> Path:
    section('portable zip')
    out = DIST / f'spotdlplus-portable-{version}.zip'
    out.unlink(missing_ok=True)
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for f in app_dir.rglob('*'):
            if f.is_file():
                z.write(f, Path(EXE_NAME) / f.relative_to(app_dir))
    say(f'wrote {out.name}')
    return out


# ---------------------------------------------------------------------------
# release archive: keep every version's exact bytes
# ---------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def _git_commit() -> str:
    try:
        r = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', '--short', 'HEAD'],
                           capture_output=True, text=True, timeout=10)
        return (r.stdout.strip() or 'unknown') if r.returncode == 0 else 'unknown'
    except Exception:  # noqa: BLE001  # no git is fine, the archive still stands
        return 'unknown'


def archive_release(version: str, artifacts: list[Path]) -> Path:
    '''
    Copy this build's artifacts into releases/<version>/, alongside a checksum
    (so you can prove the bytes) and a note on how it was built. Re-building a
    version overwrites its folder, so the archive always holds the latest build
    of each version, and every past version stays put in its own folder.
    '''
    import datetime

    section('archive')
    dest = RELEASES / version
    dest.mkdir(parents=True, exist_ok=True)

    sums = []
    for art in artifacts:
        target = dest / art.name
        shutil.copy2(art, target)
        sums.append(f'{_sha256(target)}  {art.name}')
        say(f'archived {art.name}  ({target.stat().st_size // (1024 * 1024)} MB)')

    (dest / 'SHA256SUMS.txt').write_text('\n'.join(sums) + '\n', encoding='utf-8')
    now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    info = [
        f'spotdl+ {version}',
        f'built:  {now}',
        f'git:    {_git_commit()}',
        f'python: {sys.version.split()[0]}',
        '',
        'artifacts:',
        *[f'  {a.name}  ({a.stat().st_size // (1024 * 1024)} MB)' for a in artifacts],
    ]
    (dest / 'build-info.txt').write_text('\n'.join(info) + '\n', encoding='utf-8')
    say(f'-> {dest}')
    return dest


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description='package spotdl+ for Windows')
    ap.add_argument('--portable', action='store_true',
                    help='skip the installer, just make the portable zip')
    ap.add_argument('--no-clean', action='store_true',
                    help='reuse the last PyInstaller freeze')
    ap.add_argument('--no-archive', action='store_true',
                    help='skip copying the result into releases/ (dev iterations)')
    args = ap.parse_args()

    if sys.platform != 'win32':
        say('note: a Windows .exe can only be built on Windows. Continuing anyway '
            'in case you know what you\'re doing.')

    acquire_lock()
    version = read_version()
    print(f'building {APP_NAME} {version}')

    ensure_pyinstaller()
    preflight_deps()
    found, missing = harvest_binaries()

    entry = write_entry()
    app_dir = freeze(entry, clean=not args.no_clean)
    stage_binaries(app_dir, found)
    smoke_test(app_dir)        # never wrap an installer around an exe we didn't run
    write_launcher(app_dir)

    artifacts: list[Path] = []
    if args.portable:
        artifacts.append(make_portable_zip(version, app_dir))
    else:
        installer = build_installer(version, app_dir)
        if installer is not None:
            artifacts.append(installer)
        else:
            artifacts.append(make_portable_zip(version, app_dir))

    if artifacts and not args.no_archive:
        archive_release(version, artifacts)

    section('done')
    for a in artifacts:
        say(f'-> {a}')
    if missing:
        print()
        say(f'HEADS UP: {", ".join(missing)} not bundled, so the package needs '
            'them on the target\'s PATH. Drop the .exe(s) in vendor/ and rebuild '
            'to make it fully self-contained.')


# The Inno Setup script. @TOKENS@ get filled by write_iss(). Kept here so the
# whole builder is one file you can read top to bottom.
_ISS_TEMPLATE = r'''; generated by build.py, edits here are overwritten each build
[Setup]
AppId=@APP_ID@
AppName=@APP_NAME@
AppVersion=@VERSION@
AppPublisher=@PUBLISHER@
DefaultDirName={localappdata}\Programs\spotdlplus
DefaultGroupName=@APP_NAME@
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=@OUT_DIR@
OutputBaseFilename=spotdlplus-setup-@VERSION@
Compression=lzma2
SolidCompression=yes
ChangesEnvironment=yes
WizardStyle=modern
UninstallDisplayName=@APP_NAME@
; The icon the installer, Add/Remove Programs, and shortcuts show.
SetupIconFile=@ICON@
UninstallDisplayIcon={app}\@EXE_NAME@.exe

[Messages]
; The last screen. Even if they uncheck the launch box, they leave knowing the
; one thing to type. %n is a line break.
FinishedHeadingLabel=spotdl+ is installed
FinishedLabel=You're all set.%n%nLeave the box below checked and click Finish to connect your Spotify account and see how it works.%n%nAnytime after, open a terminal (Command Prompt) and type:%n%n      spotdlp --help%n%nor just double-click the spotdl+ shortcut on your Desktop.

[Tasks]
Name: "desktopicon"; Description: "Put a spotdl+ shortcut on my Desktop"; GroupDescription: "Shortcuts:"
Name: "addtopath"; Description: "Let me type 'spotdlp' in any terminal (adds it to PATH)"; GroupDescription: "Setup:"

[Files]
Source: "@SRC_DIR@\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
; The license texts ship WITH the binary. That's obligation, not politeness. We
; bundle a GPL build of ffmpeg, and GPL §6 says whoever gets the binary gets
; the terms and a route to the source. THIRD-PARTY-NOTICES.md is that route.
Source: "@LICENSE@"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "@NOTICES@"; DestDir: "{app}"; DestName: "THIRD-PARTY-NOTICES.md"; Flags: ignoreversion

[Icons]
Name: "{group}\@APP_NAME@"; Filename: "{app}\Start.bat"; WorkingDir: "{app}"; IconFilename: "{app}\@EXE_NAME@.exe"
Name: "{userdesktop}\@APP_NAME@"; Filename: "{app}\Start.bat"; WorkingDir: "{app}"; IconFilename: "{app}\@EXE_NAME@.exe"; Tasks: desktopicon
Name: "{group}\Uninstall @APP_NAME@"; Filename: "{uninstallexe}"

[Run]
; The whole point: when Setup finishes, actually open something that TELLS the
; person what to do instead of leaving them staring at a folder they can't see.
Filename: "{app}\Start.bat"; Description: "Set up spotdl+ now (connect Spotify and see how to use it)"; WorkingDir: "{app}"; Flags: postinstall skipifsilent nowait shellexec

[Code]
const EnvKey = 'Environment';

// PATH is rebuilt in code rather than appended to from [Registry], because the
// old append was `ValueData: "{olddata};{app}"` guarded by a Check that took
// the app dir as a STRING ARGUMENT. Inno does not expand constants in a Check
// argument, so it compared PATH against the literal text and never matched,
// and every single install appended another copy. Six on my own machine.
//
// Rebuilding is idempotent: it strips every copy of our directory (and any
// empty entries, which is where the stray ';;' came from) and puts exactly one
// back. So this also REPAIRS a machine the buggy installer already polluted,
// which matters because those are already out there.
//
// Reading with RegQueryStringValue is safe on a REG_EXPAND_SZ: I probed it,
// and it hands back the raw unexpanded text, so entries like %USERPROFILE% and
// %NVM_HOME% survive the round trip intact. Writing back with
// RegWriteExpandStringValue keeps the type.
//
// (A braced Pascal comment cannot hold a constant in curly braces, the closing
// brace ends the comment early and the compiler chokes. Hence the slashes.)

// Rebuilds Path, always dropping empty entries (an empty entry means "current
// directory", which nobody wants on PATH). Every entry equal to Ours is
// dropped too, except the first one when KeepFirst is True.
function PathRebuilt(OrigPath, Ours: string; KeepFirst: Boolean): string;
var
  Rest, Item: string;
  P: Integer;
  Seen, Take: Boolean;
begin
  Result := '';
  Rest := OrigPath;
  Seen := False;
  while Length(Rest) > 0 do
  begin
    P := Pos(';', Rest);
    if P = 0 then
    begin
      Item := Rest;
      Rest := '';
    end
    else
    begin
      Item := Copy(Rest, 1, P - 1);
      Rest := Copy(Rest, P + 1, Length(Rest) - P);
    end;

    if Item = '' then
      Take := False
    else if CompareText(Item, Ours) = 0 then
    begin
      Take := KeepFirst and (not Seen);
      Seen := True;
    end
    else
      Take := True;

    if Take then
    begin
      if Result <> '' then
        Result := Result + ';';
      Result := Result + Item;
    end;
  end;
end;

// Position is the user's business. If our directory is already on PATH we
// leave it exactly where they put it and only strip the extra copies, because
// appending would silently undo a deliberate ordering. On this machine a dev
// venv shadowed the installed exe, and an append-always installer moved itself
// back behind it on every upgrade.
procedure EnsurePathOnce();
var
  OrigPath, NewPath, AppDir: string;
begin
  AppDir := ExpandConstant('{app}');
  if not RegQueryStringValue(HKCU, EnvKey, 'Path', OrigPath) then
    OrigPath := '';
  NewPath := PathRebuilt(OrigPath, AppDir, True);
  if Pos(';' + Uppercase(AppDir) + ';', ';' + Uppercase(NewPath) + ';') = 0 then
  begin
    if NewPath <> '' then
      NewPath := NewPath + ';';
    NewPath := NewPath + AppDir;
  end;
  // Only touch the registry when it would actually change something.
  if CompareStr(NewPath, OrigPath) <> 0 then
    RegWriteExpandStringValue(HKCU, EnvKey, 'Path', NewPath);
end;

procedure DropPath();
var
  OrigPath, NewPath, AppDir: string;
begin
  if not RegQueryStringValue(HKCU, EnvKey, 'Path', OrigPath) then
    exit;
  AppDir := ExpandConstant('{app}');
  // KeepFirst False: strip every copy on the way out
  NewPath := PathRebuilt(OrigPath, AppDir, False);
  if CompareStr(NewPath, OrigPath) <> 0 then
    RegWriteExpandStringValue(HKCU, EnvKey, 'Path', NewPath);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('addtopath') then
    EnsurePathOnce();
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    DropPath();
end;
'''


if __name__ == '__main__':
    main()
