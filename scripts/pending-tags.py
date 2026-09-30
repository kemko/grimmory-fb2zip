#!/usr/bin/env python3
"""Discover and publish exact patched versions using Git, gh and skopeo."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = 'https://github.com/grimmory-tools/grimmory.git'
LABEL_PREFIX = 'io.grimmory.fb2zip.'


def command(args, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, **kwargs)
    if result.returncode:
        # Do not include argv: skopeo credentials are an argument.
        raise RuntimeError(result.stderr.strip() or 'command failed')
    return result.stdout.strip()


def version(tag):
    if not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', tag):
        raise ValueError('expected stable vMAJOR.MINOR.PATCH tag')
    return tuple(map(int, tag[1:].split('.')))


def remote_tags(text, minimum):
    tags, peeled = {}, {}
    for line in text.splitlines():
        sha, ref = line.split()
        match = re.fullmatch(r'refs/tags/(v[0-9]+\.[0-9]+\.[0-9]+)(\^\{\})?', ref)
        if match and version(match[1]) >= version(minimum):
            if not re.fullmatch(r'[0-9a-f]{40}', sha):
                raise ValueError('invalid remote SHA')
            (peeled if match[2] else tags)[match[1]] = sha
    tags.update(peeled)
    return dict(sorted(tags.items(), key=lambda item: (version(item[0]), item[0])))


def configuration():
    config = json.loads((ROOT / 'upstream.json').read_text())
    if config['repository'] != UPSTREAM:
        raise ValueError('unapproved upstream')
    if type(config['patch_revision']) is not int or config['patch_revision'] < 1:
        raise ValueError('patch_revision must be a positive integer')
    return config


def series_hash():
    digest = hashlib.sha256()
    for line in (ROOT / 'patches/series').read_text().splitlines():
        name = line.strip()
        if not name or name.startswith('#'):
            continue
        if not re.fullmatch(r'[0-9]{4}-[a-z0-9-]+\.patch', name):
            raise ValueError('invalid patch name')
        data = (ROOT / 'patches' / name).read_bytes()
        digest.update(name.encode() + b'\0' + hashlib.sha256(data).digest())
    return digest.hexdigest()


def identity(tag, sha):
    config = configuration()
    if version(tag) < version(config['minimum_tag']) or not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise ValueError('unsupported tag or SHA')
    if tag == config['base_tag'] and sha != config['base_sha']:
        raise ValueError('base tag moved')
    repository = os.environ['GITHUB_REPOSITORY']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('invalid GitHub repository')
    return dict(upstream_tag=tag, upstream_sha=sha, series_sha256=series_hash(),
                patch_revision=config['patch_revision'], repository=repository,
                image_tag=f"{tag}-fb2zip.{config['patch_revision']}",
                image='ghcr.io/' + repository.split('/')[0].lower() + '/grimmory-fb2zip')


def api(path, missing=False):
    result = subprocess.run(['gh', 'api', path], capture_output=True, text=True)
    if result.returncode:
        if missing and re.search(r'HTTP 404\b', result.stderr):
            return None
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout)


def package_absent(owner):
    # Used only for explicit manual bootstrap. This list covers visible packages;
    # it cannot prove the absence of an inaccessible package.
    kind = api('users/' + owner)['type']
    namespace = 'orgs' if kind == 'Organization' else 'users'
    pages = json.loads(command(['gh', 'api', '--paginate', '--slurp',
        f'{namespace}/{owner}/packages?package_type=container&per_page=100']))
    return not any(package['name'].lower() == 'grimmory-fb2zip'
                   for page in pages for package in page)


def image_state(item):
    reference = item['image'] + ':' + item['image_tag']
    credentials = os.environ['GITHUB_ACTOR'] + ':' + os.environ['GH_TOKEN']
    base = ['skopeo', 'inspect', '--no-tags', '--creds', credentials]
    result = subprocess.run(base + ['--raw', 'docker://' + reference],
                            capture_output=True, text=True)
    if result.returncode:
        error = result.stderr
        if re.search(r'(?i)manifest unknown|name unknown|status code: 404', error):
            return None
        if re.search(r'(?i)unauthorized|status code: 401', error):
            if os.environ.get('BOOTSTRAP_PACKAGE') == 'true' and package_absent(item['repository'].split('/')[0]):
                return None
        raise RuntimeError(error.strip())
    raw = result.stdout.encode()
    manifest = json.loads(raw)
    platforms = {(m.get('platform', {}).get('os'), m.get('platform', {}).get('architecture'))
                 for m in manifest.get('manifests', [])}
    if not {('linux', 'amd64'), ('linux', 'arm64')} <= platforms:
        raise ValueError('existing image lacks required platforms')
    patch_commit = None
    digest = None
    for arch in ('amd64', 'arm64'):
        image = json.loads(command(base + ['--override-os', 'linux', '--override-arch', arch,
                                           'docker://' + reference]))
        current_digest = image['Digest']
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', current_digest) or (digest and digest != current_digest):
            raise ValueError('image digest changed during inspection')
        digest = current_digest
        labels = image.get('Labels') or {}
        for field in ('upstream_sha', 'series_sha256'):
            if labels.get(LABEL_PREFIX + field) != item[field]:
                raise ValueError('immutable image identity conflict: ' + field)
        commit = labels.get('org.opencontainers.image.revision', '')
        if not re.fullmatch(r'[0-9a-f]{40}', commit) or (patch_commit and patch_commit != commit):
            raise ValueError('invalid image patch repository revision')
        patch_commit = commit
    return dict(digest=digest, patch_commit=patch_commit)


def release_info(item):
    return api(f"repos/{item['repository']}/releases/tags/{item['image_tag']}", missing=True)


def release_manifest(item, release):
    for asset in release['assets']:
        if asset['name'] == 'build-manifest.json':
            content = command(['gh', 'api', f"repos/{item['repository']}/releases/assets/{asset['id']}",
                               '-H', 'Accept: application/octet-stream'])
            manifest = json.loads(content)
            for field in ('upstream_sha', 'series_sha256', 'image_tag', 'image'):
                if manifest.get(field) != item[field]:
                    raise ValueError('existing release identity conflict: ' + field)
            return manifest
    return None


def discover(selected=''):
    config = configuration()
    tags = remote_tags(command(['git', 'ls-remote', '--tags', UPSTREAM]), config['minimum_tag'])
    if selected:
        version(selected)
        if selected not in tags:
            raise ValueError('requested tag is absent or unsupported')
        tags = {selected: tags[selected]}
    pending = []
    for tag, sha in tags.items():
        try:
            item = identity(tag, sha)
            state = image_state(item)
            release = release_info(item) if state else None
            manifest = release_manifest(item, release) if release else None
            source_exists = release and any(a['name'] == 'patched-source.tar.gz' for a in release['assets'])
            if not selected and state and manifest and source_exists and manifest.get('image_digest') == state['digest'] and manifest.get('patch_commit') == state['patch_commit']:
                continue
        except (RuntimeError, ValueError, KeyError) as exc:
            # Keep this version in the matrix: its failure must not hide other tags.
            print(f'{tag}: {exc}', file=sys.stderr)
        pending.append(dict(tag=tag, sha=sha))
    if len(pending) > 256:
        raise ValueError('more than 256 pending versions; select tags manually before polling again')
    return pending


def source_bundle(item, state):
    upstream = ROOT / '.work/upstream'
    if not (upstream / 'LICENSE').is_file():
        raise ValueError('missing upstream LICENSE')
    # git archive includes every tracked license/notice, including optional NOTICE.
    if command(['git', 'diff', '--name-only'], cwd=upstream):
        raise ValueError('unstaged upstream changes')
    tree = command(['git', 'write-tree'], cwd=upstream)
    output = ROOT / '.work/release'
    output.mkdir(parents=True, exist_ok=True)
    tar = output / 'source.tar'
    command(['git', 'archive', '--format=tar', '--mtime=@0', '--prefix=grimmory/',
             '-o', str(tar), tree], cwd=upstream)
    archive = output / 'patched-source.tar.gz'
    archive.write_bytes(gzip.compress(tar.read_bytes(), mtime=0))
    tar.unlink()
    manifest = dict(item, source_tree=tree, source_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                    patch_commit=state['patch_commit'] if state else os.environ['GITHUB_SHA'],
                    image_digest=state['digest'] if state else None,
                    upstream_repository=UPSTREAM, platforms=['linux/amd64', 'linux/arm64'])
    (output / 'build-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return output, manifest


def publish_source(item, output, manifest):
    release = release_info(item)
    files = [output / 'patched-source.tar.gz', output / 'build-manifest.json']
    if release is None:
        command(['gh', 'release', 'create', item['image_tag'], '--repo', item['repository'],
                 '--target', os.environ['GITHUB_SHA'], '--title', item['image_tag'], '--latest=false',
                 '--notes', 'Patched source and build identity. Consult build-manifest.json for image completion.',
                 *map(str, files)])
        return
    previous = release_manifest(item, release)
    if previous:
        if previous.get('image_digest') and not manifest.get('image_digest'):
            raise ValueError('completed release image is missing; restore its recorded digest or bump patch_revision')
        for field in ('source_tree', 'source_sha256'):
            if previous.get(field) != manifest[field]:
                raise ValueError('existing source archive conflict: ' + field)
        # A documentation-only commit must not change the original build identity.
        manifest = previous
        (output / 'build-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    for asset in release['assets']:
        if asset['name'] == 'patched-source.tar.gz':
            result = subprocess.run(['gh', 'api', f"repos/{item['repository']}/releases/assets/{asset['id']}",
                                     '-H', 'Accept: application/octet-stream'], capture_output=True)
            if result.returncode:
                raise RuntimeError(result.stderr.decode(errors='replace'))
            if hashlib.sha256(result.stdout).hexdigest() != manifest['source_sha256']:
                raise ValueError('existing source archive checksum conflict')
    names = {asset['name'] for asset in release['assets']}
    for path in files:
        if path.name not in names:
            command(['gh', 'release', 'upload', item['image_tag'], str(path), '--repo', item['repository']])


def promote_latest():
    config = configuration()
    baseline = identity(config['base_tag'], config['base_sha'])
    repository = baseline['repository']
    pages = json.loads(command(['gh', 'api', '--paginate', '--slurp',
                                f'repos/{repository}/releases?per_page=100']))
    candidates = []
    for page in pages:
        for release in page:
            match = re.fullmatch(r'(v[0-9]+\.[0-9]+\.[0-9]+)-fb2zip\.([1-9][0-9]*)', release['tag_name'])
            if match and not release['draft'] and not release['prerelease']:
                candidates.append((version(match[1]), int(match[2]), release))
    # Read releases while holding the workflow's shared latest lock. Completion
    # order and retries of older versions must not move latest backwards.
    for _, revision, release in sorted(candidates, key=lambda c: c[:2], reverse=True):
        assets = {asset['name']: asset for asset in release['assets']}
        if not {'build-manifest.json', 'patched-source.tar.gz'} <= assets.keys():
            continue
        manifest = json.loads(command(['gh', 'api',
            f"repos/{repository}/releases/assets/{assets['build-manifest.json']['id']}",
            '-H', 'Accept: application/octet-stream']))
        if not manifest.get('image_digest'):
            continue
        expected = dict(repository=repository, image=baseline['image'],
                        image_tag=release['tag_name'], patch_revision=revision,
                        upstream_tag=release['tag_name'].split('-fb2zip.')[0])
        if any(manifest.get(key) != value for key, value in expected.items()):
            raise ValueError('latest release identity conflict')
        state = image_state(manifest)
        if state != dict(digest=manifest['image_digest'], patch_commit=manifest['patch_commit']):
            raise ValueError('latest release does not match its published image')
        credentials = os.environ['GITHUB_ACTOR'] + ':' + os.environ['GH_TOKEN']
        command(['skopeo', 'copy', '--all', '--preserve-digests',
                 '--src-creds', credentials, '--dest-creds', credentials,
                 f"docker://{manifest['image']}@{state['digest']}",
                 f"docker://{manifest['image']}:latest"])
        if image_state(dict(manifest, image_tag='latest')) != state:
            raise ValueError('latest digest verification failed')
        print(f"latest -> {manifest['image_tag']} ({state['digest']})")
        return
    print('No completed publication; latest unchanged')


def output_values(values):
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        for key, value in values.items():
            output.write(f'{key}={value}\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['discover', 'state', 'source', 'finalize', 'latest'])
    parser.add_argument('--tag', default='')
    parser.add_argument('--sha', default='')
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    if os.environ.get('BOOTSTRAP_PACKAGE') == 'true' and (os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch' or not args.tag or os.environ.get('GITHUB_REF') != 'refs/heads/' + os.environ.get('DEFAULT_BRANCH', '')):
        raise ValueError('package bootstrap requires a manually selected tag on the default branch')
    if args.publish and (os.environ.get('GITHUB_REF') != 'refs/heads/' + os.environ.get('DEFAULT_BRANCH', '')
                         or os.environ.get('GITHUB_EVENT_NAME') not in ('schedule', 'workflow_dispatch')):
        raise ValueError('publication requires a scheduled/manual run on the default branch')
    if args.operation == 'latest':
        if not args.publish:
            raise ValueError('latest requires --publish')
        promote_latest()
        return
    if args.operation == 'discover':
        pending = discover(args.tag)
        print(json.dumps(pending))
        output_values(dict(matrix=json.dumps(dict(include=pending)), count=len(pending)))
        return
    item = identity(args.tag, args.sha)
    state = image_state(item)
    if args.operation == 'state':
        output_values(dict(present=str(bool(state)).lower(), **item))
    elif args.operation == 'source':
        output, manifest = source_bundle(item, state)
        if args.publish:
            publish_source(item, output, manifest)
        saved = json.loads((output / 'build-manifest.json').read_text())
        output_values(dict(patch_commit=saved['patch_commit']))
    else:
        if not args.publish or not state:
            raise ValueError('finalize requires a published image and --publish')
        release = release_info(item)
        manifest = release_manifest(item, release) if release else None
        if not manifest:
            raise ValueError('publish source before finalizing image')
        if not any(asset['name'] == 'patched-source.tar.gz' for asset in release['assets']):
            raise ValueError('source archive is missing')
        if manifest.get('image_digest') not in (None, state['digest']):
            raise ValueError('published image digest changed')
        manifest['image_digest'] = state['digest']
        manifest['patch_commit'] = state['patch_commit']
        output = ROOT / '.work/release/build-manifest.json'
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(manifest, indent=2) + '\n')
        command(['gh', 'release', 'upload', item['image_tag'], str(output), '--clobber',
                 '--repo', item['repository']])


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        sys.exit('pending-tags: ' + str(exc))
