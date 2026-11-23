"""Offline contracts for the no-activation source and desktop distribution."""
import hashlib
import io
import json
from pathlib import Path
import queue
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from installer import app, core
from installer.tests.test_installer import form, Key


PAYLOAD = {'claude-cloud-solo.zip': b'fixture archive', 'install_solo.py': b'fixture helper'}


def create_payload(base):
    directory = base / 'payload'
    directory.mkdir()
    for name, data in PAYLOAD.items():
        (directory / name).write_bytes(data)
    (directory / 'manifest.json').write_text(json.dumps({
        name: hashlib.sha256(data).hexdigest() for name, data in PAYLOAD.items()
    }), encoding='utf-8')


class Runner(core.Deployer):
    """Exercise sequencing with no real SSH or HTTP connections."""
    def __init__(self, base, state):
        self.actions = []
        self.configs = []
        self.key = Key()
        self.uploads = []
        self.removed = []
        self.stop_after_connect = False
        super().__init__(base, state, lambda *a, **k: None, lambda *a: True,
                         health=lambda *a: True)

    def connect(self, settings):
        self.actions.append('connect')
        self.state.confirm_key(settings.host, self.key, self.confirm_key)
        self.client = SimpleNamespace(open_sftp=self.sftp, close=lambda: self.actions.append('close'))
        if self.stop_after_connect:
            self.cancelled.set()

    def basic(self, command, **kwargs):
        self.actions.append(command)
        return 0, '0', ''

    def sftp(self):
        self.actions.append('sftp')
        def open_upload(path, mode):
            self.uploads.append((path, mode))
            return io.BytesIO()
        return SimpleNamespace(mkdir=lambda *a, **k: None, chmod=lambda *a: None,
                               open=open_upload, remove=self.removed.append,
                               rmdir=lambda *a: None, close=lambda: None)

    def stream(self, command, config, settings, **kwargs):
        self.actions.append('deploy')
        self.configs.append(config.copy())
        return {'url': 'https://' + settings.address, 'path': '/opt/claude-cloud-solo',
                'installationId': str(uuid.UUID(config['attempt_id']))}


class OpenFlowTests(unittest.TestCase):
    def test_settings_need_only_server_and_site_credentials(self):
        settings = core.Settings.from_form(form())
        self.assertEqual(settings.host, '93.184.216.34')
        self.assertFalse(hasattr(settings, 'license_code'))

    def test_missing_or_tampered_files_prevent_connection_and_cloud_mutation(self):
        for tamper in (False, True):
            with self.subTest(tamper=tamper), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                if tamper:
                    create_payload(base)
                    (base / 'payload' / 'install_solo.py').write_bytes(b'changed')
                runner = Runner(base, core.LocalState(base / 'state.json'))
                with self.assertRaises(core.DeploymentError):
                    runner.run(core.Settings.from_form(form()))
                self.assertEqual(runner.actions, [])
                self.assertFalse((base / 'state.json').exists())

    def test_run_uses_bundled_payload_without_authorization_network_requests(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); create_payload(base)
            runner = Runner(base, core.LocalState(base / 'state.json'))
            settings = core.Settings.from_form(form())
            with patch.object(core.urllib.request, 'build_opener', side_effect=AssertionError('Unexpected HTTP')):
                result = runner.run(settings)
            self.assertIn('deploy', runner.actions)
            self.assertEqual(runner.actions[-1], 'close')
            self.assertEqual(len(runner.uploads), 2)
            self.assertEqual(set(path for path, _ in runner.uploads), set(runner.removed))
            self.assertTrue(all(mode == 'wx' for _, mode in runner.uploads))
            config = runner.configs[0]
            self.assertEqual(config['payload_sha256'], hashlib.sha256(PAYLOAD['claude-cloud-solo.zip']).hexdigest())
            self.assertEqual(result['installationId'], str(uuid.UUID(config['attempt_id'])))
            stored = (base / 'state.json').read_text(encoding='utf-8')
            self.assertNotIn(settings.site_password, stored)
            self.assertNotIn(settings.ssh_password, stored)
            self.assertEqual(set(json.loads(stored)), {'host_keys', 'attempts'})
            self.assertEqual((base / 'payload' / 'install_solo.py').read_bytes(), PAYLOAD['install_solo.py'])

    def test_failed_attempt_reuses_persisted_id_and_preserves_source_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); create_payload(base)
            settings = core.Settings.from_form(form())
            state_path = base / 'state.json'
            first = Runner(base, core.LocalState(state_path))
            first.stream = Mock(side_effect=core.DeploymentError('deployment interrupted'))
            with self.assertRaises(core.DeploymentError):
                first.run(settings)
            first_config = first.stream.call_args.args[1]
            second = Runner(base, core.LocalState(state_path))
            second.run(settings)
            self.assertEqual(first_config, second.configs[0])
            self.assertEqual((base / 'payload' / 'claude-cloud-solo.zip').read_bytes(), PAYLOAD['claude-cloud-solo.zip'])
            self.assertEqual(first.actions[-1], 'close')

    def test_other_server_gets_distinct_attempt_and_changed_key_prevents_upload(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); create_payload(base)
            state = core.LocalState(base / 'state.json')
            runner = Runner(base, state)
            settings = core.Settings.from_form(form())
            runner.run(settings)
            original = runner.configs[0]['attempt_id']
            other = state.attempt(core.Settings.from_form(form(host='cloud.example.com')))
            self.assertNotEqual(original, other)
            retry = Runner(base, core.LocalState(state.path)); retry.key = Key(b'changed')
            with self.assertRaises(core.DeploymentError):
                retry.run(settings)
            self.assertNotIn('sftp', retry.actions)

    def test_cancellation_before_upload_preserves_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary); create_payload(base)
            runner = Runner(base, core.LocalState(base / 'state.json'))
            runner.stop_after_connect = True
            with self.assertRaises(core.Cancelled):
                runner.run(core.Settings.from_form(form()))
            self.assertNotIn('sftp', runner.actions)
            self.assertEqual(runner.actions[-1], 'close')
            self.assertEqual(len(core.payload_files(base)[0]), 2)

    def test_bad_manifest_shapes_report_readable_error(self):
        for manifest in (None, [], 'invalid', {'../install.py': 'a' * 64}):
            with tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary); (base / 'payload').mkdir()
                (base / 'payload' / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
                with self.assertRaises(core.DeploymentError):
                    core.payload_files(base)


class DesktopTests(unittest.TestCase):
    def test_entry_directory_uses_executable_folder_for_frozen_distribution(self):
        with patch.object(app.sys, 'frozen', True, create=True), patch.object(app.sys, 'executable', str(ROOT / 'release' / 'start.exe')):
            self.assertEqual(app.app_directory(), ROOT / 'release')
        with patch.object(app.sys, 'frozen', False, create=True):
            self.assertEqual(app.app_directory(), ROOT / 'installer')

    def test_close_waits_for_worker_cleanup_before_destroying_window(self):
        gui = app.InstallerApp.__new__(app.InstallerApp)
        gui.busy = True; gui.closing = False
        gui.root = Mock(); gui.deployer = Mock(); gui.status = Mock()
        gui.fields = {name: Mock() for name in ('ssh_password', 'sudo_password', 'key_passphrase',
                                               'site_password', 'confirm_password')}
        gui.set_busy = Mock()
        with patch.object(app.messagebox, 'askyesno', return_value=True):
            gui.close()
        gui.deployer.cancel.assert_called_once()
        gui.root.destroy.assert_not_called()
        self.assertTrue(gui.closing)
        for field in gui.fields.values():
            field.set.assert_called_once_with('')
        gui.events = queue.Queue(); gui.events.put(('finished', {}))
        gui.poll()
        gui.root.destroy.assert_called_once()
        gui.root.after.assert_not_called()


if __name__ == '__main__':
    unittest.main()
