#!/usr/bin/env python3
"""Update GitOps repo with new package information."""

import argparse
import json
import os
import re
from pathlib import Path
from ruamel.yaml import YAML

yaml = YAML()
yaml.preserve_quotes = True

def slugify(name: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

def load_yaml(path: Path):
    if path.exists():
        with path.open() as fh:
            return yaml.load(fh) or {}
    return {}

def dump_yaml(data, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w') as fh:
        yaml.dump(data, fh)

def ensure_pkgfile(pkgfile: Path, name: str, hash_value: str):
    data = load_yaml(pkgfile)
    if not data:
        data.update({
            'apiVersion': 'v1',
            'kind': 'software',
            'spec': {
                'name': name,
                'platform': 'darwin',
                'hash_sha256': hash_value,
            },
        })
    else:
        spec = data.setdefault('spec', {})
        spec.setdefault('name', name)
        spec.setdefault('platform', 'darwin')
        spec['hash_sha256'] = hash_value
    dump_yaml(data, pkgfile)

def ensure_team(teamfile: Path, slug: str, self_service: bool):
    data = load_yaml(teamfile)
    software = data.setdefault('software', {})
    packages = software.setdefault('packages', [])
    target = f"../lib/software/{slug}.package.yml"
    for entry in packages:
        if entry.get('package_path') == target:
            entry['self_service'] = bool(self_service)
            break
    else:
        packages.append({'package_path': target, 'self_service': bool(self_service)})
    dump_yaml(data, teamfile)

def bump_policy(gitops_dir: Path, slug: str, version: str):
    policy = gitops_dir / 'lib' / 'policies' / f'update-{slug}.policy.yml'
    if not policy.exists():
        return
    text = policy.read_text()
    new_text = re.sub(r'version_compare\(".*?"\)', f'version_compare("{version}")', text)
    if text != new_text:
        policy.write_text(new_text)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--gitops-dir', required=True)
    parser.add_argument('--json', required=True)
    parser.add_argument('--teams', required=True, help='comma-separated team names')
    args = parser.parse_args()

    with open(args.json) as fh:
        data = json.load(fh)
    name = data['name']
    version = data['version']
    hash_value = data['hash']
    self_service = data.get('self_service', True)

    slug = slugify(name)
    gitops = Path(args.gitops_dir)
    pkgfile = gitops / 'lib' / 'software' / f'{slug}.package.yml'
    ensure_pkgfile(pkgfile, name, hash_value)

    for team in args.teams.split(','):
        teamfile = gitops / 'teams' / f'{team}.yml'
        ensure_team(teamfile, slug, self_service)

    bump_policy(gitops, slug, version)

if __name__ == '__main__':
    main()
