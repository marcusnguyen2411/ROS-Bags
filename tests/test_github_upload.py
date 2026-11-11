import hashlib
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import github_upload as upload


class FakeGitHub:
    def __init__(self):
        self.release = None
        self.assets = {}
        self.counts = {}
        self.fail_name = None
        self.fail_after_store = False

    def create_release(self, job, source_name):
        self.release = {'id': 17, 'tag_name': 'data-' + job['id'], 'draft': True,
                        'upload_url': 'https://uploads.github.com/example{?name}',
                        'html_url': 'https://github.com/example/release'}
        return dict(self.release)

    def get_release(self, release_id):
        return dict(self.release)

    def get_release_by_tag(self, tag):
        if tag != self.release['tag_name'] or self.release['draft']:
            raise upload.UploadError('Release not found')
        return dict(self.release)

    def list_assets(self, release_id):
        return [dict(value[0]) for value in self.assets.values()]

    def upload_asset(self, upload_url, path, name):
        self.counts[name] = self.counts.get(name, 0) + 1
        if name == self.fail_name and not self.fail_after_store:
            self.fail_name = None
            raise upload.UploadError('Simulated interruption')
        data = Path(path).read_bytes()
        asset = {'id': len(self.assets) + 1, 'name': name, 'state': 'uploaded', 'size': len(data),
                 'digest': 'sha256:' + hashlib.sha256(data).hexdigest(),
                 'browser_download_url': 'https://example.test/' + name}
        self.assets[name] = (asset, data)
        if name == self.fail_name and self.fail_after_store:
            self.fail_name = None
            raise upload.UploadError('Simulated lost response')
        return dict(asset)

    def delete_starter(self, asset_id):
        self.assets = {name: value for name, value in self.assets.items() if value[0]['id'] != asset_id}

    def publish(self, release_id):
        self.release['draft'] = False
        return dict(self.release)

    def urlopen(self, request, timeout):
        name = request.full_url.rsplit('/', 1)[-1]
        return io.BytesIO(self.assets[name][1])


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bag = self.root / 'bag with spaces'
        self.bag.mkdir()
        (self.bag / 'metadata.yaml').write_text('rosbag2_bagfile_information:\n', encoding='utf-8')
        self.data = bytes(range(256)) * 3000
        (self.bag / 'bag_0.db3').write_bytes(self.data)
        executable = self.bag / 'helper.sh'
        executable.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
        executable.chmod(0o751)
        self.job = upload.make_job(self.bag, self.root / 'state' / 'pending')
        self.client = FakeGitHub()
        patcher = mock.patch.object(upload, 'PART_BYTES', 131072)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_interrupted_upload_reuses_parts_and_publishes_complete_archive(self):
        self.client.fail_name = 'upload.tar.part000002'
        with self.assertRaisesRegex(upload.UploadError, 'interruption'):
            upload.process_job(self.job, client=self.client)
        self.assertTrue(self.client.release['draft'])
        self.assertIn('upload.tar.part000001', self.client.assets)
        self.assertTrue(self.bag.is_dir())
        url = upload.process_job(self.job, client=self.client)
        self.assertEqual(url, self.client.release['html_url'])
        self.assertFalse(self.client.release['draft'])
        self.assertEqual(self.client.counts['upload.tar.part000001'], 1)
        manifest = json.loads(self.client.assets['manifest.json'][1])
        archive_bytes = b''.join(self.client.assets[part['name']][1] for part in manifest['parts'])
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode='r:') as archive:
            self.assertEqual(archive.extractfile('bag with spaces/bag_0.db3').read(), self.data)
            self.assertIsNotNone(archive.extractfile('bag with spaces/metadata.yaml'))

    def test_lost_upload_response_does_not_duplicate_the_asset(self):
        self.client.fail_name = 'upload.tar.part000001'
        self.client.fail_after_store = True
        with self.assertRaisesRegex(upload.UploadError, 'lost response'):
            upload.process_job(self.job, client=self.client)
        upload.process_job(self.job, client=self.client)
        self.assertEqual(self.client.counts['upload.tar.part000001'], 1)
        self.assertFalse(self.client.release['draft'])

    def test_source_change_prevents_publishing_the_partial_draft(self):
        self.client.fail_name = 'upload.tar.part000002'
        with self.assertRaises(upload.UploadError):
            upload.process_job(self.job, client=self.client)
        (self.bag / 'bag_0.db3').write_bytes(b'changed')
        with self.assertRaisesRegex(upload.UploadError, 'source changed'):
            upload.process_job(self.job, client=self.client)
        self.assertTrue(self.client.release['draft'])

    def test_download_checks_parts_and_restores_bag_without_overwriting(self):
        upload.process_job(self.job, client=self.client)
        destination = self.root / 'downloaded'
        with mock.patch.object(upload, 'GitHub', return_value=self.client), \
                mock.patch.object(upload.urllib.request, 'urlopen', side_effect=self.client.urlopen):
            result = upload.download_release(self.client.release['tag_name'], destination)
            self.assertEqual((result / 'bag_0.db3').read_bytes(), self.data)
            self.assertEqual((result / 'helper.sh').stat().st_mode & 0o777, 0o751)
            with self.assertRaisesRegex(upload.UploadError, 'already exists'):
                upload.download_release(self.client.release['tag_name'], destination)
        self.assertEqual((result / 'bag_0.db3').read_bytes(), self.data)

    def test_download_rejects_corrupt_part(self):
        upload.process_job(self.job, client=self.client)
        asset, data = self.client.assets['upload.tar.part000001']
        self.client.assets['upload.tar.part000001'] = (asset, data[:-1] + b'X')
        with mock.patch.object(upload, 'GitHub', return_value=self.client), \
                mock.patch.object(upload.urllib.request, 'urlopen', side_effect=self.client.urlopen):
            with self.assertRaisesRegex(upload.UploadError, 'Checksum mismatch'):
                upload.download_release(self.client.release['tag_name'], self.root / 'downloaded')
        self.assertFalse((self.root / 'downloaded' / self.bag.name).exists())

    def test_symbolic_links_are_rejected(self):
        (self.bag / 'link').symlink_to(self.bag / 'metadata.yaml')
        with self.assertRaisesRegex(upload.UploadError, 'Symbolic links'):
            upload.process_job(self.job, client=self.client)
        self.assertIsNone(self.client.release)

    def test_checkout_job_rejects_path_outside_checkout(self):
        job = {'version': 1, 'id': 'a' * 32, 'source_type': 'checkout', 'source': '../outside'}
        with self.assertRaisesRegex(upload.UploadError, 'Invalid relative'):
            upload.source_from_job(job, self.root)

    def test_concurrent_manual_retry_is_refused(self):
        with self.job.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(upload.UploadError, 'already running'):
                upload.run_job(self.job, client=self.client)
        self.assertIsNone(self.client.release)


class AutomaticSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.checkout = self.root / '1811'
        source = self.checkout / 'ros2_ws/src/vehicle_ui_hub/src'
        source.mkdir(parents=True)
        (source / 'bag_recorder.py').write_text(
            'from github_upload_queue import queue_recording\nqueue_recording(self._bag_path)\n')
        (source / 'github_upload_queue.py').write_text('# integration\n')
        (source.parent / 'CMakeLists.txt').write_text('src/github_upload_queue.py\n')
        self.environment = mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.root / 'config')})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.client = mock.Mock()
        self.client.repo_path.return_value = '/repo'
        self.client.request.return_value = {'permissions': {'push': True}}

    def test_enables_queue_only_after_service_is_started(self):
        with mock.patch.object(upload, 'token_from_login', return_value='unused'), \
                mock.patch.object(upload, 'GitHub', return_value=self.client), \
                mock.patch.object(upload.shutil, 'which', return_value='/usr/bin/systemctl'), \
                mock.patch.object(upload.subprocess, 'run', return_value=mock.Mock(returncode=0, stderr='')) as run:
            upload.setup_automatic(self.checkout)
        marker = json.loads((self.checkout / '.rosbag-upload/enabled').read_text())
        self.assertEqual(marker['repository'], upload.REPOSITORY)
        self.assertTrue((self.checkout / '.rosbag-upload/pending').is_dir())
        self.assertEqual(len(run.call_args_list), 2)
        service = self.root / 'config/systemd/user' / marker['service']
        self.assertIn('"watch" "--checkout"', service.read_text())

    def test_service_failure_does_not_claim_automatic_uploads_are_enabled(self):
        failure = mock.Mock(returncode=1, stderr='user service unavailable')
        with mock.patch.object(upload, 'token_from_login', return_value='unused'), \
                mock.patch.object(upload, 'GitHub', return_value=self.client), \
                mock.patch.object(upload.shutil, 'which', return_value='/usr/bin/systemctl'), \
                mock.patch.object(upload.subprocess, 'run', return_value=failure):
            with self.assertRaisesRegex(upload.UploadError, 'user service unavailable'):
                upload.setup_automatic(self.checkout)
        self.assertFalse((self.checkout / '.rosbag-upload/enabled').exists())


if __name__ == '__main__':
    unittest.main()
