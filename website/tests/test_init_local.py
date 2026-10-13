"""Exercise the downloadable initializer against disposable source fixtures."""
import contextlib
import io
import re
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
INITIALIZE = runpy.run_path(str(ROOT / 'downloads/init-local-v1.1.0.py'))['main']


class InitializeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'backend').mkdir()
        (self.root / 'backend/manage.py').touch()
        (self.root / 'docker-compose.yml').touch()
        (self.root / 'pyproject.toml').write_text('[project]\nversion = "1.1.0"\n')
        (self.root / '.env.example').write_text((ROOT.parent / '.env.example').read_text())

    def initialize(self, uid=1000):
        output = io.StringIO()
        with contextlib.chdir(self.root), contextlib.redirect_stdout(output), patch('os.getuid', return_value=uid), patch('os.getgid', return_value=1000), patch('sys.platform', 'linux'):
            INITIALIZE()
        return output.getvalue()

    def test_new_configuration_is_private_and_loopback_only(self):
        output = self.initialize()
        content = (self.root / '.env').read_text()
        self.assertEqual((self.root / '.env').stat().st_mode & 0o777, 0o600)
        self.assertIn('APP_PORT=127.0.0.1:8082\n', content)
        secrets = re.findall(r'^(?:POSTGRES_PASSWORD|DJANGO_SECRET_KEY)=([a-f0-9]{64})$', content, re.M)
        self.assertEqual(len(set(secrets)), 2)
        for value in secrets:
            self.assertNotIn(value, output)
        self.assertIn('DJANGO_SESSION_COOKIE_SECURE=0\n', content)
        self.assertIn('DJANGO_CSRF_COOKIE_SECURE=0\n', content)
        self.assertTrue((self.root / 'data/analysis-runs').is_dir())
        self.assertTrue((self.root / 'workspace/rawdata').is_dir())

    def test_existing_credentials_are_preserved(self):
        (self.root / '.env').write_text('existing-configuration\n')
        with self.assertRaises(SystemExit):
            self.initialize()
        self.assertEqual((self.root / '.env').read_text(), 'existing-configuration\n')
        self.assertFalse((self.root / 'data').exists())

    def test_symlink_env_is_rejected_without_following_it(self):
        target = self.root / 'untouched'
        (self.root / '.env').symlink_to(target)
        with self.assertRaises(SystemExit):
            self.initialize()
        self.assertFalse(target.exists())

    def test_symlink_data_root_is_rejected(self):
        target = self.root / 'untouched'
        target.mkdir()
        (self.root / 'data').symlink_to(target, target_is_directory=True)
        with self.assertRaises(SystemExit):
            self.initialize()
        self.assertEqual(list(target.iterdir()), [])
        self.assertFalse((self.root / '.env').exists())

    def test_other_version_is_rejected(self):
        (self.root / 'pyproject.toml').write_text('[project]\nversion = "2.0.0"\n')
        with self.assertRaises(SystemExit):
            self.initialize()
        self.assertFalse((self.root / '.env').exists())

    def test_root_is_rejected(self):
        with self.assertRaises(SystemExit):
            self.initialize(uid=0)
        self.assertFalse((self.root / '.env').exists())


if __name__ == '__main__':
    unittest.main()
