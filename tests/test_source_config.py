import json
import os
import tempfile
import unittest
from pathlib import Path

from spiritboard.sources import load_profiles


class ConfigReadTests(unittest.TestCase):
    def test_valid_config_and_bounded_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            path.write_text(json.dumps({"profiles": []}))
            self.assertEqual(load_profiles(path), [])
            path.write_bytes(b" " * (1024 * 1024 + 1))
            with self.assertRaises(ValueError):
                load_profiles(path)

    @unittest.skipUnless(os.name == "posix", "POSIX special files")
    def test_config_symlinks_and_fifos_are_not_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.json"
            target.write_text('{"profiles": []}')
            link = root / "link.json"
            link.symlink_to(target)
            with self.assertRaises((ValueError, OSError)):
                load_profiles(link)
            fifo = root / "pipe"
            os.mkfifo(fifo)
            with self.assertRaises((ValueError, OSError)):
                load_profiles(fifo)
