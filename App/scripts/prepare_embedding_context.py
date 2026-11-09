"""Bind embedding reuse to every byte of an isolated Docker build context.

A failed main run may never publish an image. The next commit's path diff is
therefore insufficient to decide reuse. Legacy images without this fingerprint
are rebuilt once; subsequent identical contexts can reuse their exact digest.
The Dockerfile supplies all model/dependency arguments; workflow overrides are
not part of this contract. A new local COPY source must be added here or the
isolated Docker build fails rather than silently reusing an unbound input.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

INPUTS = ('.dockerignore', 'infra/embedding.Dockerfile', 'infra/embedding_service.py')


def prepare(app_root: Path, previous_fingerprint: str = '', *, changed: bool = False) -> dict:
    app_root = app_root.resolve(strict=True)
    context = app_root / '.build' / 'embedding-context'
    if context.is_symlink() or context.parent.is_symlink() or not context.resolve().is_relative_to(app_root):
        raise ValueError('Embedding context leaves the application workspace')
    payloads = {}
    for relative in INPUTS:
        source = app_root / relative
        if source.is_symlink() or not source.is_file():
            raise ValueError('Embedding input must be a regular file')
        payloads[relative] = source.read_bytes().replace(b'\r\n', b'\n')
    # Never delete an unrelated file or let it enter the fingerprinted context.
    if context.exists():
        allowed = set(INPUTS) | {'infra'}
        for path in context.rglob('*'):
            if (path.is_symlink() or path.relative_to(context).as_posix() not in allowed
                    or not (path.is_file() or path.is_dir())):
                raise ValueError('Unexpected entry in the isolated embedding context')
    context.mkdir(parents=True, exist_ok=True)
    for relative, raw in payloads.items():
        target = context / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    inputs = {name: hashlib.sha256(raw).hexdigest() for name, raw in payloads.items()}
    identity = {'schema': 'project-snow-embedding-inputs-1', 'platform': 'linux/amd64', 'files': inputs}
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'context': context.as_posix(), 'inputs_sha256': fingerprint,
            'rebuild': changed or previous_fingerprint.strip() != fingerprint, 'files': inputs}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--previous-fingerprint', default='')
    parser.add_argument('--changed', choices=('true', 'false'), default='false')
    parser.add_argument('--github-output', type=Path)
    args = parser.parse_args()
    report = prepare(args.app_root, args.previous_fingerprint, changed=args.changed == 'true')
    if args.github_output:
        with args.github_output.open('a', encoding='utf-8', newline='\n') as output:
            output.write('context=' + report['context'] + '\n')
            output.write('inputs_sha256=' + report['inputs_sha256'] + '\n')
            output.write('rebuild=' + str(report['rebuild']).lower() + '\n')
    print(json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    main()
