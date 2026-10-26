#!/usr/bin/env python3
"""One-time private Firefox clone; no credential decoding, sync or source writes."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time

SOURCE = Path.home()/'.mozilla/firefox/default-release'
DESTINATION = Path.home()/'.local/share/agent-foreman/firefox/profile'
# Session/automation identity and runtime locks never cross into the new browser.
EXCLUDE = {'lock', '.parentlock', 'parent.lock', 'MarionetteActivePort', 'DevToolsActivePort',
           'sessionstore.jsonlz4', 'sessionstore-backups', 'sessionstore-logs', 'sessionCheckpoints.json',
           'signedInUser.json', 'signedInUserOAuthTokens.json', 'weave', 'datareporting', 'crashes',
           'minidumps', 'saved-telemetry-pings', 'startupCache', 'cache2', 'shader-cache',
           'samplecourse-session-preservation-20261022', 'storage-sync-v2.sqlite'}


def fingerprint(path):
    stat = path.stat()
    return (stat.st_ino, stat.st_size, stat.st_mtime_ns)



def verify_database(db):
    try:
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RuntimeError('Cloned database failed integrity check.')
        return True
    except sqlite3.OperationalError as error:
        if 'no such collation sequence:' not in str(error): raise
        # Firefox supplies custom index comparators unavailable in system SQLite.
        # Do not substitute a wrong comparator or pretend this check passed.
        return False


def sqlite_snapshot(source, destination, deadline=3):
    """SQLite's online backup includes committed WAL data; never copy live DB bytes."""
    started = time.monotonic()
    src = sqlite3.connect(source.as_uri()+'?mode=ro', uri=True, timeout=5)
    dst = sqlite3.connect(destination)
    try:
        def progress(status, remaining, total):
            if time.monotonic()-started > deadline:
                raise TimeoutError('Profile database changed continuously; try cloning later.')
        src.backup(dst, pages=256, progress=progress, sleep=.02)
        checked = verify_database(dst)
        dst.execute('PRAGMA journal_mode=DELETE')
    finally:
        dst.close()
        src.close()
    destination.chmod(0o600)
    return checked


def stable_sqlite_snapshot(source, destination):
    """Fallback for Firefox's exclusive SQLite locks: stable DB+WAL group, recovered privately."""
    paths = [source, Path(str(source)+'-wal'), Path(str(source)+'-journal')]
    def signature(): return [fingerprint(p) if p.exists() else None for p in paths]
    for attempt in range(4):
        with tempfile.TemporaryDirectory(prefix='.sqlite-snapshot-', dir=destination.parent) as temporary:
            local = Path(temporary)/'snapshot.sqlite'
            before = signature()
            try:
                for p, token in zip(paths, before):
                    if token is not None:
                        suffix = str(p)[len(str(source)):]
                        shutil.copyfile(p, Path(str(local)+suffix))
            except FileNotFoundError:
                continue
            if before != signature(): continue
            # Any crash recovery/checkpoint writes happen only in the private temporary copy.
            db = sqlite3.connect(local)
            dst = sqlite3.connect(destination)
            try:
                checked = verify_database(db)
                db.backup(dst)
                dst.execute('PRAGMA journal_mode=DELETE')
            finally:
                dst.close();db.close()
            destination.chmod(0o600)
            return checked
    raise RuntimeError('Profile database kept changing; refusing an inconsistent clone.')



def isolate_paths(profile, source, destination):
    # Add-on and NSS metadata can contain absolute paths back into the source.
    # Rewrite only these known metadata files, never login/cookie/site contents.
    for name in ('extensions.json', 'extension-settings.json', 'pkcs11.txt', 'prefs.js', 'user.js'):
        path = profile/name
        if path.exists():
            text = path.read_text()
            path.write_text(text.replace(source.as_uri(), destination.as_uri()).replace(str(source), str(destination)))
            path.chmod(0o600)
    (profile/'addonStartup.json.lz4').unlink(missing_ok=True)


def clean_prefs(text):
    return '\n'.join(line for line in text.splitlines() if not any(
        token in line for token in ('user_pref("services.sync.', 'user_pref("identity.fxaccounts.',
                                    'user_pref("marionette.', 'user_pref("browser.sessionstore.resume_session_once"')))+'\n'


def clone(source=SOURCE, destination=DESTINATION):
    source, destination = source.resolve(), destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError('Agent profile already exists; refusing to overwrite its logins or work.')
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError('Source and destination must be separate profile directories.')
    if not source.is_dir():
        raise FileNotFoundError('Source Firefox profile not found.')
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix='.profile-clone-', dir=destination.parent))
    counts = {'files': 0, 'sqliteSnapshots': 0, 'stableSqliteSnapshots': 0, 'customCollationUnchecked': 0, 'skippedSymlinks': 0}
    try:
        # Observe encrypted credential files for concurrent replacement, never their values.
        critical = [source/name for name in ('key4.db', 'logins.json', 'logins.db', 'cert9.db') if (source/name).exists()]
        def credential_signature():
            names = ('key4.db', 'logins.json', 'logins.db', 'cert9.db')
            paths = [source/(name+suffix) for name in names for suffix in ('', '-wal', '-journal')]
            return {str(p): fingerprint(p) if p.exists() else None for p in paths}
        before = credential_signature()
        for parent, dirs, files in os.walk(source, followlinks=False):
            root = Path(parent)
            dirs[:] = [name for name in dirs if name not in EXCLUDE and not (root/name).is_symlink()]
            target = staging/root.relative_to(source);target.mkdir(parents=True, exist_ok=True, mode=0o700)
            for name in files:
                origin = root/name
                if name in EXCLUDE or name.endswith(('-wal', '-shm', '-journal', '.bak')) or '.before-' in name:
                    continue
                if origin.is_symlink():
                    counts['skippedSymlinks'] += 1;continue
                dest = target/name
                try:
                    with origin.open('rb') as stream: header = stream.read(16)
                    if header == b'SQLite format 3\x00':
                        try:
                            checked = sqlite_snapshot(origin, dest)
                        except (TimeoutError, sqlite3.OperationalError):
                            # An exclusively locked source cannot serve an online backup.
                            for suffix in ('', '-wal', '-shm', '-journal'):
                                Path(str(dest)+suffix).unlink(missing_ok=True)
                            checked = stable_sqlite_snapshot(origin, dest)
                            counts['stableSqliteSnapshots'] += 1
                        counts['sqliteSnapshots'] += 1
                        if not checked: counts['customCollationUnchecked'] += 1
                    else:
                        token = fingerprint(origin);shutil.copyfile(origin, dest);dest.chmod(0o600)
                        if fingerprint(origin) != token:
                            raise RuntimeError('A profile file changed while copying; destination not published. Retry later.')
                    counts['files'] += 1
                except FileNotFoundError:
                    # A live profile may expire cached site data. Never omit critical credentials.
                    if origin in critical: raise
                    if dest.exists(): dest.unlink()
        if credential_signature() != before:
            raise RuntimeError('Saved-login files changed during cloning; retry when no passwords are being edited.')
        isolate_paths(staging, source, destination)
        for name in ('prefs.js', 'user.js'):
            p = staging/name
            if p.exists(): p.write_text(clean_prefs(p.read_text()))
        downloads = Path.home()/'Downloads/Agent Firefox';downloads.mkdir(parents=True, exist_ok=True, mode=0o700)
        prefs = {
            'marionette.port': 2829, 'marionette.enabled': True,
            'identity.fxaccounts.enabled': False, 'services.sync.enabled': False,
            'browser.startup.page': 0, 'browser.startup.homepage': 'about:blank',
            'browser.sessionstore.resume_session_once': False,
            'browser.sessionstore.resume_from_crash': False,
            'browser.download.folderList': 2, 'browser.download.dir': str(downloads),
        }
        p = staging/'user.js'
        existing = p.read_text() if p.exists() else ''
        p.write_text(existing+'\n// Isolated agent instance; do not enable Sync.\n'+''.join(
            f'user_pref({json.dumps(k)}, {json.dumps(v)});\n' for k,v in prefs.items()))
        p.chmod(0o600)
        marker = {'source': str(source), 'createdAt': time.time(), 'port': 2829, **counts,
                  'mode': 'one-time live clone; per-database online snapshots; no cross-file transaction',
                  'sync': False}
        (staging/'agent-profile.json').write_text(json.dumps(marker, indent=2));(staging/'agent-profile.json').chmod(0o600)
        # Destination is never merged or replaced, including a concurrent initializer.
        if destination.exists() or destination.is_symlink(): raise FileExistsError('Agent profile appeared during clone.')
        staging.rename(destination)
        return marker
    finally:
        if staging.exists(): shutil.rmtree(staging)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=SOURCE);parser.add_argument('--destination', type=Path, default=DESTINATION)
    args=parser.parse_args()
    print(json.dumps(clone(args.source,args.destination), indent=2))
