"""Inspect downloaded bytes before a skill is distributed or a tool is run."""

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import msvcrt
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import time
from urllib.request import urlopen
import uuid
import zipfile

from management_inventory import ROOT, digest


SECURITY = ROOT / '_tools' / 'vendor' / 'install-security'
STATE = ROOT / '.skill-management' / 'install-security'
SKILLSHARE = '0.30.13'
CLAMAV = '1.5.5'
MAX_FILE = 1024 ** 3
MAX_DB_AGE = 7 * 24 * 60 * 60
DAILY = 24 * 60 * 60
RELEASES = (
    (f'skillshare-{SKILLSHARE}',
     f'https://github.com/runkids/skillshare/releases/download/v{SKILLSHARE}/skillshare_{SKILLSHARE}_windows_amd64.zip',
     '093a5380580bff3c33c265f961be737f0e3b73cdea49bbd338b2481617274462'),
    (f'clamav-{CLAMAV}',
     f'https://github.com/Cisco-Talos/clamav/releases/download/clamav-{CLAMAV}/clamav-{CLAMAV}.win.x64.zip',
     '90169f5e551010bfbd0208b50d9b2fa8988ef396cccbd46b0cc8cae03e22a4a4'),
)
SKILLSHARE_HASH = 'bf99288c3c3683de2bc7311f7fd5cb5b792171cc7528caf495e4dfb78a711f5b'
CLAMSCAN_HASH = '0662897fc4a4bdc2e27edc0a2740b78b94a5e78588af5cc60f6d04fe02d96be0'
FRESHCLAM_HASH = '63092fb85ab8dbc685d77448d50480f2b27af01a83e5b4c9da689c0f85eef5c1'
SOURCE_SUFFIXES = {'.bat', '.cmd', '.js', '.mjs', '.ps1', '.py', '.sh', '.ts', '.vbs'}


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def path_stat(path):
    info = path.lstat()
    if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise ValueError(f'Linked or reparse-point candidate: {path}')
    if stat.S_ISREG(info.st_mode):
        if info.st_size > MAX_FILE:
            raise ValueError(f'File exceeds the 1 GiB scan limit: {path}')
    elif not stat.S_ISDIR(info.st_mode):
        raise ValueError(f'Unsupported candidate file type: {path}')
    return info


def snapshot(path):
    if stat.S_ISREG(path_stat(path).st_mode):
        return sha256(path), 1
    entries = []

    def walk_error(error):
        raise error

    for directory, folders, files in os.walk(path, onerror=walk_error):
        for name in folders + files:
            entry = Path(directory) / name
            info = path_stat(entry)
            row = {'path': entry.relative_to(path).as_posix(), 'type': 'directory'}
            if stat.S_ISREG(info.st_mode):
                row.update(type='file', bytes=info.st_size, sha256=sha256(entry))
            entries.append(row)
    return digest(sorted(entries, key=lambda row: row['path'])), sum(row['type'] == 'file' for row in entries)


def engine(directory, name, expected):
    exe = SECURITY / directory / name
    if not exe.is_file():
        raise RuntimeError('Scanner unavailable. Run Manage-Skills.cmd scan-setup.')
    if sha256(exe) != expected:
        raise RuntimeError(f'Scanner integrity check failed: {exe}')
    return exe


def run(exe, *args, timeout, env=None):
    return subprocess.run([str(exe), *map(str, args)], cwd=exe.parent, env=env,
                          capture_output=True, text=True, encoding='utf-8', errors='replace',
                          timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW)


@contextlib.contextmanager
def database_lock():
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / 'clamav.lock').open('a+b') as lock:
        lock.seek(0)
        for attempt in range(120):
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if attempt == 119:
                    raise RuntimeError('ClamAV is busy; retry the scan.') from None
                time.sleep(1)
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def refresh_database(force=False):
    db = STATE / 'clamdb'
    db.mkdir(exist_ok=True)
    stamp = STATE / 'last-refresh'
    warning = None
    now = time.time()
    if force or not stamp.exists() or not 0 <= now - stamp.stat().st_mtime < DAILY:
        config = STATE / 'freshclam.conf'
        config.write_text('DatabaseMirror database.clamav.net\nConnectTimeout 12\n'
                          'ReceiveTimeout 45\nTestDatabases yes\n', encoding='ascii')
        try:
            updater = engine(f'clamav-{CLAMAV}', 'freshclam.exe', FRESHCLAM_HASH)
            refreshed = run(updater, f'--config-file={config}',
                            f'--datadir={db}', '--quiet', '--stdout', timeout=180)
            if refreshed.returncode:
                warning = (refreshed.stdout + refreshed.stderr).strip()[-600:] or f'freshclam exit {refreshed.returncode}'
            else:
                stamp.touch()
        except (OSError, subprocess.TimeoutExpired) as exc:
            warning = str(exc)
    daily = list(db.glob('daily.c[vl]d'))
    if len(daily) != 1 or not any(db.glob('main.c[vl]d')) or not any(db.glob('bytecode.c[vl]d')):
        raise RuntimeError(f'ClamAV signatures are incomplete. {warning or "Run scan-setup."}')
    with daily[0].open('rb') as stream:
        header = stream.read(512).decode('ascii').strip().split(':')
    if header[0] != 'ClamAV-VDB' or not 0 <= time.time() - int(header[8]) <= MAX_DB_AGE:
        raise RuntimeError(f'ClamAV daily signatures are stale or invalid. {warning or "Run scan-setup."}')
    info = {'version': CLAMAV, 'daily_version': header[2], 'daily_published': header[1]}
    if warning:
        info['refresh_warning'] = warning
    return db, info


def malware_scan(path, expected_files):
    exe = engine(f'clamav-{CLAMAV}', 'clamscan.exe', CLAMSCAN_HASH)
    with database_lock():
        db, info = refresh_database()
        scan = run(exe, f'--database={db}', '--recursive=yes', '--infected',
                   '--follow-dir-symlinks=0', '--follow-file-symlinks=0',
                   '--alert-encrypted=yes', '--alert-exceeds-max=yes',
                   '--max-filesize=1024M', '--max-scansize=1536M', path, timeout=300)
    diagnostics = (scan.stdout + '\n' + scan.stderr).splitlines()
    counts = re.search(r'^Scanned files:\s*(\d+)', scan.stdout, re.MULTILINE)
    errors = re.search(r'^Total errors:\s*[1-9]\d*', scan.stdout, re.MULTILINE)
    info['scanned_files'] = int(counts[1]) if counts else 0
    info['findings'] = [line.strip() for line in diagnostics if line.rstrip().endswith(' FOUND')][:30]
    if scan.returncode == 1:
        info['status'] = 'blocked'
    elif scan.returncode or errors or info['scanned_files'] < expected_files or scan.stderr.strip():
        info.update(status='incomplete', diagnostics=[line.strip() for line in diagnostics if line.strip()][-20:])
    else:
        info['status'] = 'passed'
    return info


def skill_audit(path):
    exe = engine(f'skillshare-{SKILLSHARE}', 'skillshare.exe', SKILLSHARE_HASH)
    with tempfile.TemporaryDirectory(dir=STATE) as temp:
        env = {**os.environ, **dict.fromkeys(('XDG_CONFIG_HOME', 'XDG_DATA_HOME',
                                             'XDG_STATE_HOME', 'XDG_CACHE_HOME'), temp)}
        env.pop('SKILLSHARE_CONFIG', None)
        scan = run(exe, 'audit', path, '--profile', 'strict', '--format', 'json', '--no-tui', timeout=120, env=env)
    data = json.loads(scan.stdout)
    summary = data['summary']
    findings = [{key: finding[key] for key in ('ruleId', 'severity', 'file', 'line') if key in finding}
                for result in (data.get('results') or []) for finding in (result.get('findings') or [])]
    result = {'version': SKILLSHARE, 'summary': summary, 'findings': findings}
    if scan.returncode == 1 or summary.get('high') or summary.get('critical'):
        result['status'] = 'blocked'
    elif scan.returncode != 0 or not summary.get('scanned') or summary.get('scanErrors') or data.get('incomplete'):
        result.update(status='incomplete', reason='Skillshare did not complete the audit.')
    elif any(finding.get('ruleId') == 'bundled-config-unparsed' for finding in findings):
        result.update(status='incomplete', reason='Skillshare could not read bundled settings or hooks.')
    elif summary.get('oversizedFiles') and summary.get('skippedFiles', 0) > summary.get('binaryFiles', 0):
        result.update(status='incomplete', reason='Skillshare skipped oversized content that may contain instructions.')
    else:
        result['status'] = 'passed'
    return result


def inspect_candidate(candidate, receipt_dir, *, skill=False, expected_sha=None):
    path = Path(candidate).absolute()
    result = {'path': str(path), 'scanned_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'status': 'incomplete'}
    try:
        sha, files = snapshot(path)
        result.update(sha256=sha, files=files)
        if not files:
            raise ValueError('Candidate contains no files.')
        if expected_sha and sha != expected_sha.casefold():
            result['status'] = 'blocked'
            raise ValueError('SHA-256 mismatch.')
        result['clamav'] = malware_scan(path, files)
        result['status'] = result['clamav']['status']
        if result['status'] == 'passed' and (skill or path.is_dir() or path.name == 'SKILL.md' or path.suffix.lower() in SOURCE_SUFFIXES):
            result['skillshare'] = skill_audit(path)
            result['status'] = result['skillshare']['status']
        if snapshot(path) != (sha, files):
            result['status'] = 'incomplete'
            raise ValueError('Candidate changed during scanning.')
    except (OSError, ValueError, RuntimeError, KeyError, IndexError, subprocess.SubprocessError) as exc:
        if result['status'] != 'blocked':
            result['status'] = 'incomplete'
        result['error'] = str(exc)
    receipt_dir.mkdir(parents=True, exist_ok=True)
    receipt = receipt_dir / f'{uuid.uuid4().hex}.json'
    result['receipt'] = str(receipt)
    receipt.write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    return result


def provision():
    SECURITY.mkdir(parents=True, exist_ok=True)
    with database_lock(), tempfile.TemporaryDirectory(dir=SECURITY) as temp:
        for directory, url, expected in RELEASES:
            destination = SECURITY / directory
            if destination.exists():
                continue
            archive = Path(temp) / 'release.zip'
            with urlopen(url, timeout=90) as response, archive.open('wb') as output:
                shutil.copyfileobj(response, output)
            if sha256(archive) != expected:
                raise RuntimeError(f'Release SHA-256 mismatch: {directory}')
            staged = Path(temp) / directory
            staged.mkdir()
            with zipfile.ZipFile(archive) as package:
                for member in package.infolist():
                    member_path = PurePosixPath(member.filename)
                    name = member_path.name
                    if member_path.parent.name == 'certs' and name == 'clamav.crt':
                        target = staged / 'certs' / name
                    elif name in ('skillshare.exe', 'clamscan.exe', 'freshclam.exe', 'COPYING.txt', 'LICENSE') or name.endswith('.dll'):
                        target = staged / name
                    else:
                        continue
                    target.parent.mkdir(exist_ok=True)
                    with package.open(member) as source, target.open('xb') as output:
                        shutil.copyfileobj(source, output)
            os.replace(staged, destination)
        engine(f'skillshare-{SKILLSHARE}', 'skillshare.exe', SKILLSHARE_HASH)
        engine(f'clamav-{CLAMAV}', 'clamscan.exe', CLAMSCAN_HASH)
        _, database = refresh_database(force=True)
        return {'skillshare': SKILLSHARE, 'clamav': database, 'location': str(SECURITY)}


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument('action', choices=('scan', 'scan-setup'))
    cli.add_argument('path', nargs='?')
    cli.add_argument('--expected-sha256')
    args = cli.parse_args()
    if args.action == 'scan-setup':
        try:
            result = provision()
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            print(json.dumps({'status': 'incomplete', 'error': str(exc)}, ensure_ascii=False))
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if not args.path:
        cli.error('scan requires a file or directory path')
    if args.expected_sha256 and not re.fullmatch(r'[a-fA-F0-9]{64}', args.expected_sha256):
        cli.error('expected SHA-256 must be 64 hexadecimal characters')
    result = inspect_candidate(args.path, STATE / 'receipts', expected_sha=args.expected_sha256)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return {'passed': 0, 'blocked': 1, 'incomplete': 2}[result['status']]


if __name__ == '__main__':
    raise SystemExit(main())
