#!/usr/bin/env bash
set -euo pipefail

# Python handles paths and arguments without shell interpolation.
exec python3 - "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)" "$@" <<'PY'
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(sys.argv.pop(1))
APPROVED_UPSTREAM = 'https://github.com/grimmory-tools/grimmory.git'


def git(*args, cwd=None):
    return subprocess.run(
        ['git', '-c', 'core.hooksPath=/dev/null', *args], cwd=cwd,
        check=True, text=True, stdout=subprocess.PIPE,
    ).stdout.strip()


def version(tag):
    if not isinstance(tag, str) or not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', tag):
        raise ValueError('expected a stable tag in vMAJOR.MINOR.PATCH format')
    return tuple(map(int, tag[1:].split('.')))


def prepare(args):
    manifest = json.loads((ROOT / 'upstream.json').read_text())
    if manifest['repository'] != APPROVED_UPSTREAM:
        raise ValueError('upstream repository is not approved')
    if version(args.tag) < version(manifest['minimum_tag']):
        raise ValueError('tag predates the minimum supported version')
    expected = args.expected_sha
    if args.tag == manifest['base_tag']:
        if expected and expected != manifest['base_sha']:
            raise ValueError('expected SHA conflicts with the pinned base SHA')
        expected = manifest['base_sha']
    if not expected or not re.fullmatch(r'[0-9a-f]{40}', expected):
        raise ValueError('a full expected commit SHA is required for non-base tags')

    requested = Path(args.destination)
    if '..' in requested.parts:
        raise ValueError('destination must not contain ..')
    destination = Path(os.path.abspath(requested))
    work = ROOT / '.work'
    if work.is_symlink() or destination.resolve() == work or work not in destination.resolve().parents:
        raise ValueError('destination must be a new directory inside this repository .work/')
    if destination.exists() or destination.is_symlink():
        raise ValueError('destination already exists; existing checkouts are never modified')

    source = APPROVED_UPSTREAM
    if args.local_source:
        source_path = Path(args.local_source)
        if not source_path.is_absolute() or not source_path.is_dir():
            raise ValueError('--local-source requires an absolute local Git directory')
        source = str(source_path.resolve())
        git('rev-parse', '--git-dir', cwd=source)

    patches = []
    for line in (ROOT / 'patches/series').read_text().splitlines():
        name = line.strip()
        if not name or name.startswith('#'):
            continue
        if not re.fullmatch(r'[0-9]{4}-[a-z0-9-]+\.patch', name):
            raise ValueError('unsafe patch name in patches/series')
        patch = ROOT / 'patches' / name
        if patch.is_symlink() or not patch.is_file() or patch in patches:
            raise ValueError('patch must be an existing, unique regular file: ' + name)
        header = patch.read_text().split('diff --git', 1)[0]
        for dependency in re.findall(r'^Depends-on:\s*(.+)$', header, re.MULTILINE):
            if dependency.strip() not in [item.name for item in patches]:
                raise ValueError('patch dependency must appear earlier in series: ' + dependency.strip())
        patches.append(patch)

    # Reserve a fresh path. Cleanup applies only to the directory created here.
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir()
    try:
        git('init', '--quiet', str(destination))
        git('remote', 'add', 'origin', source, cwd=destination)
        git('fetch', '--no-tags', '--depth=1', 'origin',
            'refs/tags/' + args.tag, cwd=destination)
        actual = git('rev-parse', 'FETCH_HEAD^{commit}', cwd=destination)
        if actual != expected:
            raise ValueError(f'upstream tag moved or wrong source: expected {expected}, got {actual}')
        git('checkout', '--quiet', '--detach', actual, cwd=destination)
        if git('status', '--porcelain', cwd=destination):
            raise ValueError('upstream checkout is not clean')
        for patch in patches:
            print('Applying ' + patch.name, flush=True)
            git('apply', '--check', str(patch), cwd=destination)
            git('apply', '--index', str(patch), cwd=destination)
        print(f'Prepared {args.tag} ({actual}) with {len(patches)} patches at {destination}')
    except BaseException:
        shutil.rmtree(destination)
        raise


parser = argparse.ArgumentParser(description='Prepare a fresh, verified and patched upstream checkout.')
parser.add_argument('tag')
parser.add_argument('destination', help='new path inside this repository .work/')
parser.add_argument('expected_sha', nargs='?', help='mandatory for every tag except the pinned base tag')
parser.add_argument('--local-source', help='absolute local Git repository for offline development/tests')
try:
    prepare(parser.parse_args())
except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as exc:
    sys.exit('prepare-upstream: ' + str(exc))
PY
