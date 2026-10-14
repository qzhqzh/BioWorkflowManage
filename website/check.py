#!/usr/bin/env python3
"""Check the built public artifact without starting application services."""
import hashlib
import json
import os
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'dist'
CONFIG = json.loads((ROOT / 'site.json').read_text())


class Page(HTMLParser):
    def __init__(self, path):
        super().__init__()
        self.path, self.ids, self.links, self.h1s = path, set(), [], 0
        self.feed(path.read_text())

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'h1':
            self.h1s += 1
        if 'id' in attrs:
            assert attrs['id'] not in self.ids, f'Duplicate ID: {self.path}: {attrs["id"]}'
            self.ids.add(attrs['id'])
        for attr in ('href', 'src'):
            if attr in attrs:
                self.links.append(attrs[attr])
        assert not any(key.startswith('on') for key in attrs), f'Inline handler: {self.path}'


def main():
    pages = {path: Page(path) for path in OUT.rglob('*.html')}
    assert len(pages) == 12, 'Build the complete site first'
    for path, page in pages.items():
        assert page.h1s == 1, f'Expected one page heading: {path}'
        for link in page.links:
            url = urlsplit(link)
            assert url.scheme in ('', 'https', 'http'), f'Unexpected URL scheme: {link}'
            if url.netloc or url.scheme:
                continue
            assert link, f'Empty link: {path}'
            target = OUT / unquote(url.path).lstrip('/') if url.path.startswith('/') else path.parent / unquote(url.path)
            if not url.path:
                target = path
            elif target.is_dir():
                target = target / 'index.html'
            target = target.resolve()
            assert target.is_relative_to(OUT.resolve()), f'Escaped output: {link}'
            assert target.is_file(), f'Broken link: {path.relative_to(OUT)} -> {link}'
            if url.fragment and target in pages:
                assert unquote(url.fragment) in pages[target].ids, f'Broken anchor: {path} -> {link}'
    expected = subprocess.run(
        ['git', 'show', CONFIG['release_commit'] + ':schemas/integration-openapi-v1.json'],
        cwd=ROOT.parent, capture_output=True, check=True,
    ).stdout
    actual = (OUT / 'openapi.json').read_bytes()
    assert actual == expected, 'Public OpenAPI must match the released contract exactly'
    assert json.loads(actual)['info']['version'] == CONFIG['api_document_version']
    manifest = json.loads((OUT / 'version.json').read_text())
    assert manifest['api_sha256'] == hashlib.sha256(expected).hexdigest()
    for line in (OUT / 'downloads/SHA256SUMS').read_text().splitlines():
        digest, name = line.split('  ')
        assert hashlib.sha256((OUT / 'downloads' / name).read_bytes()).hexdigest() == digest
    for path in OUT.rglob('*.json'):
        json.loads(path.read_text())
    # Extra private labels can be passed locally; never store them in public source.
    denied = [value.casefold() for value in os.environ.get('BWM_SITE_DENY_TERMS', '').split('|') if value]
    for path in OUT.rglob('*'):
        if not path.is_file():
            continue
        if path.suffix in ('.png', '.woff2', '.ttf'):
            data = path.read_bytes()
            signatures = {'.png': b'\x89PNG\r\n\x1a\n', '.woff2': b'wOF2', '.ttf': b'\x00\x01\x00\x00'}
            assert path.is_relative_to(OUT / 'assets'), f'Unexpected binary download: {path}'
            assert data.startswith(signatures[path.suffix]), f'Invalid asset: {path}'
            continue
        content = path.read_text()
        assert not any(term in content.casefold() for term in denied), f'Private label found: {path}'
        assert not re.search(r'/home/[^\s/]+/', content), f'Internal host path: {path}'
        assert not re.search(r'https?://[^\s/<>"`]+\.(?:internal|local)(?:[:/\s"`]|$)', content), f'Internal host: {path}'
        assert not re.search(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', content), f'Private key: {path}'
    print(f'PASS: {len(pages)} pages, local links and anchors, released OpenAPI, downloads and public-content checks')


if __name__ == '__main__':
    main()
