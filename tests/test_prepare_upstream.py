import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PrepareUpstreamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'patch repo'
        (self.root / 'scripts').mkdir(parents=True)
        (self.root / 'patches').mkdir()
        shutil.copy(ROOT / 'scripts/prepare-upstream.sh', self.root / 'scripts')
        (self.root / 'patches/series').write_text('')
        self.source = Path(self.temp.name) / 'upstream'
        self.source.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        (self.source / 'book.txt').write_text('original\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'fixture')
        self.sha = self.git('rev-parse', 'HEAD')
        self.git('tag', '-a', 'v3.5.0', '-m', 'annotated fixture')
        self.manifest = {
            'repository': 'https://github.com/grimmory-tools/grimmory.git',
            'base_tag': 'v3.5.0', 'base_sha': self.sha,
            'minimum_tag': 'v3.5.0', 'patch_revision': 1,
        }
        self.write_manifest()
        self.destination = self.root / '.work/upstream'

    def write_manifest(self):
        (self.root / 'upstream.json').write_text(json.dumps(self.manifest))

    def git(self, *args, cwd=None):
        return subprocess.run(['git', *args], cwd=cwd or self.source, check=True,
                              capture_output=True, text=True).stdout.strip()

    def run_prepare(self, tag='v3.5.0', destination=None, expected=None):
        command = ['bash', str(self.root / 'scripts/prepare-upstream.sh'),
                   tag, str(destination or self.destination)]
        if expected is not None:
            command.append(expected)
        command += ['--local-source', str(self.source)]
        return subprocess.run(command, cwd=self.root, capture_output=True, text=True)

    def patch(self, name, after):
        (self.source / 'book.txt').write_text(after)
        patch = self.git('diff') + '\n'
        (self.root / 'patches' / name).write_text(patch)
        self.git('add', '.')
        self.git('commit', '-qm', name)
        return name

    def test_empty_series_annotated_tag_and_uncommitted_source_ignored(self):
        (self.source / 'book.txt').write_text('uncommitted\n')
        result = self.run_prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.destination / 'book.txt').read_text(), 'original\n')
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=self.destination), self.sha)
        self.assertEqual(self.git('status', '--porcelain', cwd=self.destination), '')

    def test_existing_destination_is_untouched_clean_or_dirty(self):
        self.assertEqual(self.run_prepare().returncode, 0)
        for dirty in (False, True):
            with self.subTest(dirty=dirty):
                if dirty:
                    (self.destination / 'book.txt').write_text('user edit\n')
                before = (self.destination / 'book.txt').read_bytes()
                result = self.run_prepare()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('already exists', result.stderr)
                self.assertEqual((self.destination / 'book.txt').read_bytes(), before)

    def test_ordered_patches_are_staged(self):
        names = [self.patch('0001-first.patch', 'first\n'),
                 self.patch('0002-second.patch', 'second\n')]
        (self.root / 'patches/series').write_text('\n'.join(names) + '\n')
        result = self.run_prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.destination / 'book.txt').read_text(), 'second\n')
        self.assertEqual(self.git('diff', cwd=self.destination), '')
        self.assertIn('+second', self.git('diff', '--cached', cwd=self.destination))

    def test_dependency_must_precede_dependent_patch(self):
        first = self.patch('0001-first.patch', 'first\n')
        second = self.patch('0002-second.patch', 'second\n')
        path = self.root / 'patches' / second
        path.write_text('Depends-on: ' + first + '\n\n' + path.read_text())
        for series in (second, second + '\n' + first):
            (self.root / 'patches/series').write_text(series)
            result = self.run_prepare()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('dependency must appear earlier', result.stderr)
            self.assertFalse(self.destination.exists())
        (self.root / 'patches/series').write_text(first + '\n' + second)
        self.assertEqual(self.run_prepare().returncode, 0)

    def test_actual_ui_patch_requires_backend_patch(self):
        name = '0002-allow-fb2zip-in-upload-ui.patch'
        shutil.copy(ROOT / 'patches' / name, self.root / 'patches' / name)
        (self.root / 'patches/series').write_text(name)
        result = self.run_prepare()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('dependency must appear earlier', result.stderr)
        self.assertFalse(self.destination.exists())

    def test_partial_patch_failure_removes_only_new_checkout(self):
        first = self.patch('0001-first.patch', 'first\n')
        second = self.patch('0002-second.patch', 'second\n')
        correct_second = (self.root / 'patches' / second).read_text()
        (self.root / 'patches' / second).write_text((self.root / 'patches' / first).read_text())
        (self.root / 'patches/series').write_text(first + '\n' + second + '\n')
        sibling = self.root / '.work/keep.txt'
        sibling.parent.mkdir()
        sibling.write_text('keep')
        result = self.run_prepare()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.destination.exists())
        self.assertEqual(sibling.read_text(), 'keep')
        (self.root / 'patches' / second).write_text(correct_second)
        self.assertEqual(self.run_prepare().returncode, 0)

    def test_moved_tag_or_mismatched_sha_fails_closed(self):
        self.manifest['base_sha'] = 'a' * 40
        self.write_manifest()
        result = self.run_prepare()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('expected', result.stderr)
        self.assertFalse(self.destination.exists())

    def test_nonbase_tag_requires_expected_commit(self):
        self.git('tag', 'v3.6.0')
        self.assertNotEqual(self.run_prepare(tag='v3.6.0').returncode, 0)
        result = self.run_prepare(tag='v3.6.0', expected=self.sha)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_base_pin_cannot_be_overridden(self):
        result = self.run_prepare(expected='a' * 40)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('conflicts', result.stderr)

    def test_invalid_tag_and_old_tag_rejected(self):
        for tag in ('v3.5.0-rc1', 'HEAD', 'v3.4.0', 'v3.5.0;touch injected', '../v3.5.0'):
            with self.subTest(tag=tag):
                self.assertNotEqual(self.run_prepare(tag=tag).returncode, 0)
                self.assertFalse(self.destination.exists())
        self.assertFalse((self.root / 'injected').exists())

    def test_unsafe_destinations_rejected(self):
        for path in (self.root, self.root / '.work', self.root / '.work/../escape',
                     Path(self.temp.name) / 'outside'):
            with self.subTest(path=path):
                self.assertNotEqual(self.run_prepare(destination=path).returncode, 0)
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        (self.root / '.work').symlink_to(outside)
        self.assertNotEqual(self.run_prepare().returncode, 0)
        self.assertEqual(list(outside.iterdir()), [])

    def test_unsafe_or_missing_patch_rejected_before_checkout(self):
        for name in ('../outside.patch', '/tmp/0001-x.patch', '0001-missing.patch'):
            with self.subTest(name=name):
                (self.root / 'patches/series').write_text(name)
                self.assertNotEqual(self.run_prepare().returncode, 0)
                self.assertFalse(self.destination.exists())

    def test_unapproved_repository_rejected(self):
        self.manifest['repository'] = 'https://example.invalid/upstream.git'
        self.write_manifest()
        self.assertNotEqual(self.run_prepare().returncode, 0)
        self.assertFalse(self.destination.exists())


if __name__ == '__main__':
    unittest.main()
