#!/usr/bin/env python3
"""Install a checksum-pinned restic binary to a caller-selected private directory."""
import argparse
import bz2
import hashlib
from pathlib import Path
import platform
import urllib.request

VERSION = '0.19.1'
SUMS = {
    ('Linux', 'x86_64'): ('linux_amd64', 'f415415624dcc452f2a02b8c33641791a8c6d6d3b65bbb3543fcf9a25151585c'),
    ('Linux', 'aarch64'): ('linux_arm64', 'a5f64aaab53d51e311fa3829124c5b703f2d14cf187d8640b6be3b2b49376465'),
    ('Darwin', 'arm64'): ('darwin_arm64', '7be0a144ccc377880f294204aa271d76e4b79554b42a751151d425ce6ebac143'),
}

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--directory', type=Path, required=True)
args = parser.parse_args()
asset, checksum = SUMS[(platform.system(), platform.machine())]
url = f'https://github.com/restic/restic/releases/download/v{VERSION}/restic_{VERSION}_{asset}.bz2'
with urllib.request.urlopen(url, timeout=60) as response:
    compressed = response.read()
if hashlib.sha256(compressed).hexdigest() != checksum:
    raise ValueError('Official restic binary checksum mismatch')
args.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
destination = args.directory / 'restic'
with destination.open('xb') as stream:
    stream.write(bz2.decompress(compressed))
destination.chmod(0o700)
