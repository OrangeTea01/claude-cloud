"""Build public source, local installer payload and optional Windows release.

Run from any directory. No network, credentials or activation service required.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = ('backend', 'frontend', 'deploy', 'scripts', 'tests', 'translation', '.github', 'installer')
ROOT_FILES = ('compose.yaml', 'Dockerfile', 'requirements.txt', 'requirements-dev.txt',
              '.env.example', '.gitignore', '.dockerignore', 'README.md', 'DEPLOYMENT.md',
              'SOLO_GUIDE.md', 'BEGINNER_GUIDE.md', 'API.md', 'LICENSE', 'NOTICE',
              'THIRD_PARTY.md', 'open-login-browser.cmd')
EXCLUDE_DIRS = {'.git', 'node_modules', '__pycache__', '.pytest_cache', 'dist', '.venv',
                'payload', '.build', 'build', 'data', '.preview-data', '.test-deps',
                '.claude', '.ssh', '.aws', '.codex', 'secrets', 'backups'}
EXCLUDE_SUFFIXES = {'.pyc', '.pyo', '.exe', '.zip', '.db', '.sqlite', '.sqlite3', '.log', '.key', '.pem', '.spec'}
PRIVATE_NAMES = {'credentials.json', '.credentials.json', 'state.json', 'known_hosts',
                 '.oneclick.json', '.oneclick.compose.yaml'}


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def source_files():
    result = []
    for name in ROOT_FILES:
        path = ROOT / name
        if path.is_file() and not path.is_symlink():
            result.append(path)
    for name in SOURCE_DIRS:
        for path in (ROOT / name).rglob('*'):
            relative = path.relative_to(ROOT)
            if any(part in EXCLUDE_DIRS or part.startswith('.venv') for part in relative.parts):
                continue
            if any(parent.is_symlink() for parent in [path, *path.parents] if parent != ROOT.parent):
                continue
            if not path.is_file() or path.suffix.lower() in EXCLUDE_SUFFIXES:
                continue
            if path.name.lower() in PRIVATE_NAMES or any(path.name.lower().endswith(extension + ending)
                    for extension in ('.db', '.sqlite', '.sqlite3') for ending in ('-wal', '-shm', '-journal')):
                continue
            if path.name.startswith(('.env', '.smoke-', 'id_rsa', 'id_ed25519')):
                continue
            result.append(path)
    return sorted(set(result))


def archive(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as output:
        for source, name in sorted(entries, key=lambda item: item[1]):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            output.writestr(info, source.read_bytes())
    with zipfile.ZipFile(temporary) as check:
        if check.testzip() is not None:
            raise RuntimeError('Archive integrity check failed')
    temporary.replace(path)
    return path


def build(payload_only=False, windows=False):
    for required in ('LICENSE', 'backend/app.py', 'scripts/install_solo.py', 'installer/core.py'):
        if not (ROOT / required).is_file():
            raise SystemExit(f'Missing source file: {required}')
    files = source_files()
    payload = ROOT / 'installer' / 'payload'
    payload.mkdir(parents=True, exist_ok=True)
    website = [p for p in files if p.relative_to(ROOT).parts[0] not in {'installer', '.github'}]
    archive(payload / 'claude-cloud-solo.zip', [(p, 'claude-cloud/' + p.relative_to(ROOT).as_posix()) for p in website])
    shutil.copyfile(ROOT / 'scripts/install_solo.py', payload / 'install_solo.py')
    hashes = {name: sha(payload / name) for name in ('claude-cloud-solo.zip', 'install_solo.py')}
    (payload / 'manifest.json').write_text(json.dumps(hashes, indent=2) + '\n', encoding='utf-8')
    outputs = [str(payload)]
    if payload_only:
        return outputs
    dist = ROOT / 'dist'
    source_zip = archive(dist / 'claude-cloud-opensource-source.zip',
                         [(p, 'claude-cloud-opensource/' + p.relative_to(ROOT).as_posix()) for p in files])
    outputs.append(str(source_zip))
    release_files = [source_zip]
    if windows:
        executable = ROOT / 'installer/dist/开始部署.exe'
        if not executable.is_file():
            raise SystemExit('Build installer/dist/开始部署.exe with installer/build.ps1 first.')
        entries = [(executable, '开始部署.exe'), (source_zip, 'source/claude-cloud-opensource-source.zip')]
        entries.extend((payload / n, 'payload/' + n) for n in (*hashes, 'manifest.json'))
        entries.extend((ROOT / n, n) for n in ('LICENSE', 'NOTICE', 'THIRD_PARTY.md', 'README.md', 'DEPLOYMENT.md', 'SOLO_GUIDE.md') if (ROOT / n).is_file())
        entries.extend((ROOT / 'installer' / n, 'installer-' + n) for n in ('README.md', 'requirements.txt', 'THIRD_PARTY_NOTICES.txt'))
        release_files.append(archive(dist / 'claude-cloud-opensource-windows.zip',
                                     [(p, 'claude-cloud-opensource-windows/' + n) for p, n in entries]))
        outputs.append(str(release_files[-1]))
    (dist / 'SHA256SUMS.txt').write_text(''.join(f'{sha(p)}  {p.name}\n' for p in release_files), encoding='utf-8')
    return outputs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--payload-only', action='store_true')
    group.add_argument('--windows', action='store_true')
    args = parser.parse_args()
    for output in build(args.payload_only, args.windows):
        print(output)


if __name__ == '__main__':
    main()
