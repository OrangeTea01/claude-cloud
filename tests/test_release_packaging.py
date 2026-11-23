"""Distribution must not collect runtime state even inside a source directory."""
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from scripts import build_release


class ReleasePackagingTests(TestCase):
    def test_allowlisted_roots_exclude_credentials_databases_and_build_artifacts(self):
        allowed = {'README.md', '.env.example', 'backend/app.py', 'installer/core.py', 'frontend/package.json'}
        denied = {'.env', 'random.txt', 'seller-assets/payload.json', 'backend/auth.sqlite3',
                  'backend/auth.sqlite3-wal', 'backend/auth.db-shm', 'backend/auth.db-journal',
                  'backend/credentials.json', 'backend/.credentials.json', 'installer/state.json',
                  'installer/payload/manifest.json', 'installer/dist/开始部署.exe',
                  'backend/.claude/session.json', 'scripts/.ssh/id_ed25519',
                  'frontend/node_modules/package/index.js', 'frontend/dist/index.html',
                  'backend/.env.backup', 'scripts/.smoke-password'}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in allowed | denied:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('fixture', encoding='utf-8')
            with patch.object(build_release, 'ROOT', root):
                names = {p.relative_to(root).as_posix() for p in build_release.source_files()}
            self.assertEqual(names, allowed)

    def test_archive_content_and_hash_are_reproducible(self):
        import zipfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'example.txt'; source.write_text('hello', encoding='utf-8')
            first = build_release.archive(root / 'first.zip', [(source, 'project/example.txt')])
            second = build_release.archive(root / 'second.zip', [(source, 'project/example.txt')])
            self.assertEqual(build_release.sha(first), build_release.sha(second))
            with zipfile.ZipFile(first) as archive:
                self.assertEqual(archive.namelist(), ['project/example.txt'])
                self.assertEqual(archive.read('project/example.txt'), b'hello')
