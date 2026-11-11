import fcntl
import importlib.util
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "fetch_bag.py"
SPEC = importlib.util.spec_from_file_location("fetch_bag", SCRIPT)
fetch_bag = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fetch_bag)


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("rsync"), "requires Linux and rsync")
class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote folder" / "bag with spaces"
        self.remote.mkdir(parents=True)
        (self.remote / "metadata.yaml").write_text("rosbag2_bagfile_information:\n", encoding="utf-8")
        self.payload = bytes(range(256)) * 4096
        (self.remote / "bag_0.db3").write_bytes(self.payload)
        (self.remote / "nested").mkdir()
        (self.remote / "nested" / "other file").write_text("extra", encoding="utf-8")
        self.dest = self.root / "local folder"
        self.source = f"testuser@mockhost:{self.remote}"

        # Run the real rsync sender locally through a stand-in for SSH. This
        # exercises rsync's remote protocol and quoting without network access.
        fakebin = self.root / "bin"
        fakebin.mkdir()
        ssh = fakebin / "ssh"
        ssh.write_text(
            '#!/bin/sh\nif [ "$1" = "-l" ]; then shift 2; fi\nshift\nexec "$@"\n',
            encoding="utf-8",
        )
        ssh.chmod(0o755)
        patcher = mock.patch.dict(os.environ, {"PATH": f"{fakebin}:{os.environ['PATH']}"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_real_rsync_copies_entire_bag_with_spaces(self):
        fetch_bag.fetch(self.source, self.dest)
        finished = self.dest / self.remote.name
        self.assertEqual((finished / "bag_0.db3").read_bytes(), self.payload)
        self.assertEqual((finished / "nested" / "other file").read_text(), "extra")
        self.assertTrue((finished / "metadata.yaml").is_file())
        self.assertFalse((self.dest / f".{self.remote.name}.partial").exists())

    def test_interruption_retains_partial_and_rerun_completes(self):
        def interrupted(command, check):
            target = Path(command[-1])
            (target / "bag_0.db3").write_bytes(self.payload[:131072])
            return mock.Mock(returncode=12)

        with mock.patch.object(fetch_bag.subprocess, "run", side_effect=interrupted):
            with self.assertRaisesRegex(fetch_bag.TransferError, "rsync exited"):
                fetch_bag.fetch(self.source, self.dest)
        partial = self.dest / f".{self.remote.name}.partial" / "bag" / "bag_0.db3"
        self.assertEqual(partial.stat().st_size, 131072)
        fetch_bag.fetch(self.source, self.dest)
        self.assertEqual((self.dest / self.remote.name / "bag_0.db3").read_bytes(), self.payload)

    def test_existing_destination_is_not_modified(self):
        existing = self.dest / self.remote.name
        existing.mkdir(parents=True)
        (existing / "important").write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(fetch_bag.TransferError, "already exists"):
            fetch_bag.fetch(self.source, self.dest)
        self.assertEqual((existing / "important").read_text(), "keep")
        self.assertEqual(list(existing.iterdir()), [existing / "important"])

    def test_partial_from_another_source_is_not_reused(self):
        def interrupted(command, check):
            return mock.Mock(returncode=12)

        with mock.patch.object(fetch_bag.subprocess, "run", side_effect=interrupted):
            with self.assertRaises(fetch_bag.TransferError):
                fetch_bag.fetch(self.source, self.dest)
        with self.assertRaisesRegex(fetch_bag.TransferError, "different source"):
            fetch_bag.fetch(f"other@mockhost:{self.remote}", self.dest)

    def test_missing_metadata_keeps_partial_until_it_is_available(self):
        (self.remote / "metadata.yaml").unlink()
        with self.assertRaisesRegex(fetch_bag.TransferError, "metadata.yaml"):
            fetch_bag.fetch(self.source, self.dest)
        self.assertFalse((self.dest / self.remote.name).exists())
        self.assertTrue((self.dest / f".{self.remote.name}.partial" / "bag" / "bag_0.db3").exists())
        (self.remote / "metadata.yaml").write_text("rosbag2_bagfile_information:\n")
        fetch_bag.fetch(self.source, self.dest)
        self.assertTrue((self.dest / self.remote.name / "metadata.yaml").is_file())

    def test_concurrent_transfer_is_refused(self):
        partial = self.dest / f".{self.remote.name}.partial"
        partial.mkdir(parents=True)
        with (partial / ".lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(fetch_bag.TransferError, "Another transfer"):
                fetch_bag.fetch(self.source, self.dest)

    def test_atomic_publish_never_replaces_existing_directory(self):
        source = self.root / "source"
        source.mkdir()
        existing = self.root / "existing"
        existing.mkdir()
        with self.assertRaisesRegex(fetch_bag.TransferError, "already exists"):
            fetch_bag.rename_without_replacing(source, existing)
        self.assertTrue(source.is_dir())
        self.assertTrue(existing.is_dir())


class SourceTests(unittest.TestCase):
    def test_requires_remote_absolute_bag_path(self):
        for source in ("/tmp/bag", "host:relative/bag", "host:/", "-bad:/tmp/bag", "host:/tmp/../bag"):
            with self.subTest(source=source), self.assertRaises(fetch_bag.TransferError):
                fetch_bag.parse_source(source)

    def test_strips_trailing_slashes(self):
        self.assertEqual(fetch_bag.parse_source("karbon:/bags/test///"), ("karbon:/bags/test", "test"))


if __name__ == "__main__":
    unittest.main()
