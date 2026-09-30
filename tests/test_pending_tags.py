import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location('pending_tags', Path(__file__).resolve().parents[1] / 'scripts/pending-tags.py')
tags = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tags)
SHA = 'a' * 40
COMMIT = 'b' * 40
DIGEST = 'sha256:' + 'd' * 64


class PendingTagsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = dict(repository=tags.UPSTREAM, base_tag='v3.5.0', base_sha=SHA,
                           minimum_tag='v3.5.0', patch_revision=1)
        self.write_config()
        (self.root / 'patches').mkdir()
        (self.root / 'patches/series').write_text('0001-test.patch\n')
        (self.root / 'patches/0001-test.patch').write_text('fixture patch\n')
        self.addCleanup(patch.stopall)
        patch.object(tags, 'ROOT', self.root).start()
        patch.dict(os.environ, dict(GITHUB_REPOSITORY='Owner/grimmory-fb2zip', GITHUB_ACTOR='actor',
                   GH_TOKEN='fixture-token', GITHUB_SHA=COMMIT, GITHUB_REF='refs/heads/main',
                   DEFAULT_BRANCH='main', GITHUB_EVENT_NAME='workflow_dispatch',
                   GITHUB_OUTPUT=str(self.root / 'output'), BOOTSTRAP_PACKAGE='false')).start()
        self.item = tags.identity('v3.5.0', SHA)

    def write_config(self):
        (self.root / 'upstream.json').write_text(json.dumps(self.config))

    def result(self, stdout='', stderr='', returncode=0):
        return subprocess.CompletedProcess([], returncode, stdout, stderr)

    def existing(self):
        raw = json.dumps(dict(manifests=[dict(platform=dict(os='linux', architecture=a)) for a in ('amd64', 'arm64')])) + '\n'
        info = json.dumps(dict(Digest=DIGEST, Labels={
            tags.LABEL_PREFIX + 'upstream_sha': SHA,
            tags.LABEL_PREFIX + 'series_sha256': self.item['series_sha256'],
            'org.opencontainers.image.revision': COMMIT}))
        return [self.result(raw), self.result(info), self.result(info)]

    def test_remote_sort_filter_and_annotated_peeling(self):
        text = '\n'.join(f'{sha}\trefs/tags/{tag}' for tag, sha in [
            ('v3.10.0', SHA), ('v3.9.0', SHA), ('v3.5.0', COMMIT),
            ('v3.5.0^{}', SHA), ('v3.4.9', SHA), ('v4.0.0-rc1', SHA), ('other', SHA)])
        self.assertEqual(list(tags.remote_tags(text, 'v3.5.0')), ['v3.5.0', 'v3.9.0', 'v3.10.0'])
        self.assertEqual(tags.remote_tags(text, 'v3.5.0')['v3.5.0'], SHA)

    def test_invalid_remote_sha_rejected(self):
        with self.assertRaises(ValueError):
            tags.remote_tags('invalid refs/tags/v3.5.0', 'v3.5.0')

    def test_base_move_and_unsafe_tags_rejected(self):
        for tag, sha in [('v3.5.0', COMMIT), ('v3.4.9', SHA), ('v3.5.0;exit', SHA)]:
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                tags.identity(tag, sha)

    def test_revision_changes_image_tag_and_patch_changes_identity(self):
        self.config['patch_revision'] = 2
        self.write_config()
        (self.root / 'patches/0001-test.patch').write_text('changed patch\n')
        item = tags.identity('v3.5.0', SHA)
        self.assertEqual(item['image_tag'], 'v3.5.0-fb2zip.2')
        self.assertNotEqual(item['series_sha256'], self.item['series_sha256'])

    def test_documentation_commit_does_not_change_identity(self):
        os.environ['GITHUB_SHA'] = 'c' * 40
        self.assertEqual(tags.identity('v3.5.0', SHA), self.item)

    def test_registry_digest_is_authoritative_not_hash_of_cli_output(self):
        with patch.object(tags.subprocess, 'run', side_effect=self.existing()):
            self.assertEqual(tags.image_state(self.item), dict(digest=DIGEST, patch_commit=COMMIT))

    def test_identity_conflict_fails_for_each_architecture(self):
        for index in (1, 2):
            responses = self.existing()
            responses[index].stdout = responses[index].stdout.replace(SHA, 'c' * 40)
            with self.subTest(index=index), patch.object(tags.subprocess, 'run', side_effect=responses):
                with self.assertRaisesRegex(ValueError, 'identity conflict'):
                    tags.image_state(self.item)

    def test_missing_platform_rejected(self):
        with patch.object(tags.subprocess, 'run', return_value=self.result('{"manifests":[]}')):
            with self.assertRaisesRegex(ValueError, 'platforms'):
                tags.image_state(self.item)

    def test_manifest_missing_is_pending(self):
        with patch.object(tags.subprocess, 'run', return_value=self.result(stderr='manifest unknown', returncode=1)):
            self.assertIsNone(tags.image_state(self.item))

    def test_access_and_network_errors_are_not_missing(self):
        for error in ('unauthorized', 'status code: 403', 'connection timed out', 'TLS failure'):
            with self.subTest(error=error), patch.object(tags.subprocess, 'run', return_value=self.result(stderr=error, returncode=1)):
                with self.assertRaises(RuntimeError):
                    tags.image_state(self.item)

    def test_bootstrap_only_handles_401_with_successful_empty_listing(self):
        os.environ['BOOTSTRAP_PACKAGE'] = 'true'
        with patch.object(tags.subprocess, 'run', return_value=self.result(stderr='unauthorized', returncode=1)):
            with patch.object(tags, 'package_absent', return_value=True):
                self.assertIsNone(tags.image_state(self.item))
            with patch.object(tags, 'package_absent', return_value=False):
                with self.assertRaises(RuntimeError):
                    tags.image_state(self.item)
            with patch.object(tags, 'package_absent', side_effect=RuntimeError('API forbidden')):
                with self.assertRaisesRegex(RuntimeError, 'API forbidden'):
                    tags.image_state(self.item)
        with patch.object(tags.subprocess, 'run', return_value=self.result(stderr='status code: 403', returncode=1)):
            with self.assertRaises(RuntimeError):
                tags.image_state(self.item)

    def test_package_listing_checks_every_page(self):
        with patch.object(tags, 'api', return_value={'type': 'Organization'}):
            with patch.object(tags, 'command', return_value='[[], [{"name":"grimmory-fb2zip"}]]'):
                self.assertFalse(tags.package_absent('Owner'))

    def discovery(self, number=3):
        mapping = {f'v3.{i+5}.0': SHA for i in range(number)}
        patch.object(tags, 'command', return_value='').start()
        patch.object(tags, 'remote_tags', return_value=mapping).start()
        return mapping

    def test_all_pending_tags_returned(self):
        mapping = self.discovery()
        with patch.object(tags, 'image_state', return_value=None):
            self.assertEqual(tags.discover(), [dict(tag=t, sha=s) for t, s in mapping.items()])

    def test_one_version_error_does_not_hide_other_versions(self):
        self.discovery()
        with patch.object(tags, 'image_state', side_effect=[ValueError('moved tag'), None, None]), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(len(tags.discover()), 3)

    def test_matrix_overflow_fails_explicitly(self):
        self.discovery(257)
        with patch.object(tags, 'image_state', return_value=None), self.assertRaisesRegex(ValueError, '256'):
            tags.discover()

    def test_complete_image_skipped_but_manual_tag_rechecked(self):
        self.discovery(1)
        with patch.object(tags, 'image_state', return_value=dict(digest=DIGEST, patch_commit=COMMIT)), \
             patch.object(tags, 'release_info', return_value={'assets': [{'name': 'patched-source.tar.gz'}]}), \
             patch.object(tags, 'release_manifest', return_value=dict(image_digest=DIGEST, patch_commit=COMMIT)):
            self.assertEqual(tags.discover(), [])
            self.assertEqual(len(tags.discover('v3.5.0')), 1)

    def test_partial_release_is_pending(self):
        self.discovery(1)
        with patch.object(tags, 'image_state', return_value=dict(digest=DIGEST, patch_commit=COMMIT)), \
             patch.object(tags, 'release_info', return_value={'assets': []}), \
             patch.object(tags, 'release_manifest', return_value=None):
            self.assertEqual(len(tags.discover()), 1)

    def make_source(self):
        upstream = self.root / '.work/upstream'
        upstream.mkdir(parents=True)
        def git(*args):
            subprocess.run(['git', *args], cwd=upstream, check=True, capture_output=True)
        git('init', '-q')
        git('config', 'user.name', 'Fixture')
        git('config', 'user.email', 'fixture@example.invalid')
        (upstream / 'LICENSE').write_text('Fixture license\n')
        (upstream / 'app.txt').write_text('unpatched\n')
        git('add', '.')
        git('commit', '-qm', 'fixture')
        (upstream / 'app.txt').write_text('patched\n')
        git('add', 'app.txt')
        return upstream

    def test_source_contains_staged_patch_and_license_is_deterministic(self):
        self.make_source()
        output, manifest = tags.source_bundle(self.item, None)
        first = (output / 'patched-source.tar.gz').read_bytes()
        with tarfile.open(output / 'patched-source.tar.gz') as archive:
            self.assertEqual(archive.extractfile('grimmory/app.txt').read(), b'patched\n')
            self.assertEqual(archive.extractfile('grimmory/LICENSE').read(), b'Fixture license\n')
        tags.source_bundle(self.item, None)
        self.assertEqual(first, (output / 'patched-source.tar.gz').read_bytes())
        self.assertEqual(hashlib.sha256(first).hexdigest(), manifest['source_sha256'])

    def test_unstaged_source_rejected(self):
        upstream = self.make_source()
        (upstream / 'app.txt').write_text('unverified\n')
        with self.assertRaisesRegex(ValueError, 'unstaged'):
            tags.source_bundle(self.item, None)

    def test_source_retry_only_uploads_missing_asset_preserving_commit(self):
        self.make_source()
        output, manifest = tags.source_bundle(self.item, None)
        previous = dict(manifest, patch_commit='c' * 40)
        with patch.object(tags, 'release_info', return_value={'assets': [{'name': 'build-manifest.json'}]}), \
             patch.object(tags, 'release_manifest', return_value=previous), patch.object(tags, 'command') as cmd:
            tags.publish_source(self.item, output, manifest)
            self.assertEqual(cmd.call_count, 1)
            self.assertIn(str(output / 'patched-source.tar.gz'), cmd.call_args.args[0])
            self.assertNotIn('--clobber', cmd.call_args.args[0])
        self.assertEqual(json.loads((output / 'build-manifest.json').read_text())['patch_commit'], 'c' * 40)

    def test_completed_release_with_deleted_image_cannot_be_republished(self):
        self.make_source()
        output, manifest = tags.source_bundle(self.item, None)
        previous = dict(manifest, image_digest=DIGEST)
        with patch.object(tags, 'image_state', return_value=None), \
             patch.object(tags, 'release_info', return_value={'assets': [{'name': 'build-manifest.json'}]}), \
             patch.object(tags, 'release_manifest', return_value=previous), \
             patch.object(tags, 'command') as cmd, \
             patch.object(tags.sys, 'argv', ['pending-tags.py', 'source', '--tag', 'v3.5.0', '--sha', SHA, '--publish']):
            # Source publication runs before registry login and image push in the workflow.
            with patch.object(tags, 'source_bundle', return_value=(output, manifest)):
                with self.assertRaisesRegex(ValueError, 'completed release image is missing'):
                    tags.main()
            cmd.assert_not_called()
        self.assertFalse((self.root / 'output').exists())

    def test_source_retry_checks_existing_archive_before_adding_manifest(self):
        self.make_source()
        output, manifest = tags.source_bundle(self.item, None)
        with patch.object(tags, 'release_info', return_value={'assets': [{'name': 'patched-source.tar.gz', 'id': 1}]}), \
             patch.object(tags, 'release_manifest', return_value=None), \
             patch.object(tags.subprocess, 'run', return_value=self.result(b'wrong source', b'')), \
             patch.object(tags, 'command') as cmd:
            with self.assertRaisesRegex(ValueError, 'checksum conflict'):
                tags.publish_source(self.item, output, manifest)
            cmd.assert_not_called()

    def test_manifest_identity_conflict_rejected(self):
        wrong = dict(self.item, upstream_sha=COMMIT)
        with patch.object(tags, 'command', return_value=json.dumps(wrong)):
            with self.assertRaisesRegex(ValueError, 'identity conflict'):
                tags.release_manifest(self.item, {'assets': [{'name': 'build-manifest.json', 'id': 1}]})

    def test_publish_guard_rejects_pr_and_nondefault_branch(self):
        for variable, value in [('GITHUB_EVENT_NAME', 'pull_request'), ('GITHUB_REF', 'refs/heads/other')]:
            with patch.dict(os.environ, {variable: value}), \
                 patch.object(tags.sys, 'argv', ['pending-tags.py', 'source', '--tag', 'v3.5.0', '--sha', SHA, '--publish']):
                with self.assertRaisesRegex(ValueError, 'default branch'):
                    tags.main()

    def test_bootstrap_requires_manual_tag_on_default_branch(self):
        os.environ['BOOTSTRAP_PACKAGE'] = 'true'
        for args, env in [(['discover'], {}), (['discover', '--tag', 'v3.5.0'], {'GITHUB_EVENT_NAME': 'schedule'}),
                          (['discover', '--tag', 'v3.5.0'], {'GITHUB_REF': 'refs/heads/other'})]:
            with patch.dict(os.environ, env), patch.object(tags.sys, 'argv', ['pending-tags.py', *args]):
                with self.assertRaisesRegex(ValueError, 'bootstrap'):
                    tags.main()

    def test_dry_source_has_no_release_writes(self):
        self.make_source()
        with patch.object(tags, 'image_state', return_value=None), patch.object(tags, 'publish_source') as publish, \
             patch.object(tags.sys, 'argv', ['pending-tags.py', 'source', '--tag', 'v3.5.0', '--sha', SHA]):
            tags.main()
            publish.assert_not_called()
        self.assertTrue((self.root / '.work/release/build-manifest.json').is_file())

    def latest_fixtures(self, entries):
        releases, manifests = [], {}
        for tag, revision, digest in entries:
            name = f'{tag}-fb2zip.{revision}'
            manifests[name] = dict(self.item, upstream_tag=tag, patch_revision=revision,
                                   image_tag=name, image_digest=digest, patch_commit=COMMIT)
            releases.append(dict(tag_name=name, draft=False, prerelease=False, assets=[
                dict(name='build-manifest.json', id=name), dict(name='patched-source.tar.gz')]))
        def response(args):
            if '--paginate' in args:
                return json.dumps([[release] for release in releases])
            if args[:2] == ['gh', 'api']:
                return json.dumps(manifests[args[2].split('/')[-1]])
            return ''
        return releases, manifests, response

    def test_latest_selects_numeric_version_then_revision_across_pages(self):
        for entries in [
            [('v3.10.0', 1, DIGEST), ('v3.9.0', 20, DIGEST)],
            [('v3.10.0', 10, DIGEST), ('v3.10.0', 9, DIGEST)],
        ]:
            with self.subTest(entries=entries):
                releases, manifests, response = self.latest_fixtures(entries)
                with patch.object(tags, 'command', side_effect=response) as cmd, \
                     patch.object(tags, 'image_state', return_value=dict(digest=DIGEST, patch_commit=COMMIT)) as inspect, \
                     contextlib.redirect_stdout(io.StringIO()):
                    tags.promote_latest()
                self.assertEqual(inspect.call_args_list[0].args[0]['image_tag'], releases[0]['tag_name'])
                self.assertEqual(inspect.call_args_list[1].args[0]['image_tag'], 'latest')
                copy = cmd.call_args_list[-1].args[0]
                self.assertEqual(copy[:4], ['skopeo', 'copy', '--all', '--preserve-digests'])
                self.assertEqual(copy[-2:], [f"docker://{self.item['image']}@{DIGEST}",
                                            f"docker://{self.item['image']}:latest"])

    def test_latest_ignores_incomplete_draft_and_prerelease_publications(self):
        releases, manifests, response = self.latest_fixtures([
            ('v3.5.0', 1, DIGEST), ('v3.6.0', 1, None), ('v3.7.0', 1, DIGEST),
            ('v3.8.0', 1, DIGEST), ('v3.9.0', 1, DIGEST)])
        releases[2]['draft'] = True
        releases[3]['prerelease'] = True
        releases[4]['assets'].pop()
        with patch.object(tags, 'command', side_effect=response), \
             patch.object(tags, 'image_state', return_value=dict(digest=DIGEST, patch_commit=COMMIT)) as inspect, \
             contextlib.redirect_stdout(io.StringIO()):
            tags.promote_latest()
        self.assertEqual(inspect.call_args_list[0].args[0]['image_tag'], releases[0]['tag_name'])

    def test_latest_without_completed_release_does_not_touch_registry(self):
        _, _, response = self.latest_fixtures([('v3.5.0', 1, None)])
        with patch.object(tags, 'command', side_effect=response) as cmd, \
             patch.object(tags, 'image_state') as inspect, contextlib.redirect_stdout(io.StringIO()):
            tags.promote_latest()
        inspect.assert_not_called()
        self.assertTrue(all(call.args[0][0] == 'gh' for call in cmd.call_args_list))

    def test_latest_rejects_wrong_identity_or_registry_state_before_copy(self):
        for field, value in [('image', 'ghcr.io/other/package'), ('patch_revision', 2),
                             ('image_digest', 'sha256:' + 'e' * 64), ('patch_commit', SHA)]:
            releases, manifests, response = self.latest_fixtures([('v3.5.0', 1, DIGEST)])
            manifests[releases[0]['tag_name']][field] = value
            with self.subTest(field=field), patch.object(tags, 'command', side_effect=response) as cmd, \
                 patch.object(tags, 'image_state', return_value=dict(digest=DIGEST, patch_commit=COMMIT)), \
                 self.assertRaises(ValueError):
                tags.promote_latest()
            self.assertTrue(all(call.args[0][0] == 'gh' for call in cmd.call_args_list))

    def test_latest_propagates_missing_image_and_registry_errors(self):
        for state in [None, RuntimeError('registry unavailable')]:
            _, _, response = self.latest_fixtures([('v3.5.0', 1, DIGEST)])
            with self.subTest(state=state), patch.object(tags, 'command', side_effect=response) as cmd, \
                 patch.object(tags, 'image_state', side_effect=[state]), \
                 self.assertRaises((ValueError, RuntimeError)):
                tags.promote_latest()
            self.assertTrue(all(call.args[0][0] == 'gh' for call in cmd.call_args_list))

    def test_latest_checks_destination_digest_after_copy(self):
        _, _, response = self.latest_fixtures([('v3.5.0', 1, DIGEST)])
        with patch.object(tags, 'command', side_effect=response), \
             patch.object(tags, 'image_state', side_effect=[dict(digest=DIGEST, patch_commit=COMMIT), None]), \
             self.assertRaisesRegex(ValueError, 'verification failed'):
            tags.promote_latest()

    def test_latest_requires_publish_and_default_branch(self):
        for args, env in [(['latest'], {}), (['latest', '--publish'], {'GITHUB_EVENT_NAME': 'pull_request'}),
                          (['latest', '--publish'], {'GITHUB_REF': 'refs/heads/other'})]:
            with patch.dict(os.environ, env), patch.object(tags.sys, 'argv', ['pending-tags.py', *args]), \
                 patch.object(tags, 'promote_latest') as promote, self.assertRaises(ValueError):
                tags.main()
            promote.assert_not_called()

    def test_finalize_recovers_after_image_push_without_rebuilding(self):
        with patch.object(tags, 'image_state', return_value=dict(digest=DIGEST, patch_commit=COMMIT)), \
             patch.object(tags, 'release_info', return_value={'assets': [{'name': 'patched-source.tar.gz'}]}), \
             patch.object(tags, 'release_manifest', return_value=dict(self.item, image_digest=None)), \
             patch.object(tags, 'command') as cmd, \
             patch.object(tags.sys, 'argv', ['pending-tags.py', 'finalize', '--tag', 'v3.5.0', '--sha', SHA, '--publish']):
            tags.main()
            self.assertEqual(cmd.call_count, 1)
            self.assertEqual(cmd.call_args.args[0][:3], ['gh', 'release', 'upload'])
        result = json.loads((self.root / '.work/release/build-manifest.json').read_text())
        self.assertEqual(result['image_digest'], DIGEST)

    def test_finalize_does_not_rewrite_known_digest(self):
        with patch.object(tags, 'image_state', return_value=dict(digest=DIGEST, patch_commit=COMMIT)), \
             patch.object(tags, 'release_info', return_value={'assets': [{'name': 'patched-source.tar.gz'}]}), \
             patch.object(tags, 'release_manifest', return_value=dict(self.item, image_digest='sha256:' + 'e' * 64)), \
             patch.object(tags, 'command') as cmd, \
             patch.object(tags.sys, 'argv', ['pending-tags.py', 'finalize', '--tag', 'v3.5.0', '--sha', SHA, '--publish']):
            with self.assertRaisesRegex(ValueError, 'digest changed'):
                tags.main()
            cmd.assert_not_called()


if __name__ == '__main__':
    unittest.main()
