"""Run a checksum-pinned Gitleaks release, with redacted diagnostics."""
import argparse
import hashlib
from pathlib import Path
import platform
import secrets
import subprocess
import tarfile
import tempfile
from urllib.request import urlopen

VERSION = '8.30.1'
ARCHIVES = {
    ('Linux', 'x86_64'): ('linux_x64', '551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb'),
    ('Darwin', 'arm64'): ('darwin_arm64', 'b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5'),
    ('Darwin', 'x86_64'): ('darwin_x64', 'dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709'),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['git', 'dir'], default='git')
    parser.add_argument('path', nargs='?', default='.')
    args = parser.parse_args()
    archive_name, checksum = ARCHIVES[(platform.system(), platform.machine())]
    url = (f'https://github.com/gitleaks/gitleaks/releases/download/v{VERSION}/'
           f'gitleaks_{VERSION}_{archive_name}.tar.gz')
    with tempfile.TemporaryDirectory(prefix='motionsound-gitleaks-') as folder:
        root = Path(folder)
        with urlopen(url, timeout=60) as response:
            archive = response.read()
        if hashlib.sha256(archive).hexdigest() != checksum:
            raise RuntimeError('Gitleaks archive checksum mismatch')
        bundle_path = root / 'gitleaks.tar.gz'
        bundle_path.write_bytes(archive)
        with tarfile.open(bundle_path) as bundle:
            bundle.extract(bundle.getmember('gitleaks'), root, filter='data')
        command = [str(root / 'gitleaks'), '--redact', '--no-banner']
        fixture = root / 'positive-control'
        fixture.mkdir()
        (fixture / 'settings.py').write_text('api_key = "' + secrets.token_hex(32) + '"\n')
        result = subprocess.run([*command, 'dir', str(fixture)], capture_output=True)
        if result.returncode != 1:
            raise RuntimeError('Gitleaks positive control failed to detect the synthetic secret')
        print('Gitleaks positive control: passed', flush=True)
        subprocess.run([*command, args.mode, args.path], check=True)


if __name__ == '__main__':
    main()
