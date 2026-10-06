#!/usr/bin/env python3
"""Restrict host deployment secrets and snapshots without changing app storage."""
import argparse
import os
from pathlib import Path
import stat
import sys


def secure(directory, check=False, lock_only=False):
    # Descriptor-relative operations never follow symlinks, including snapshot
    # entries. Nonblocking opens let us reject special files without hanging.
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK

    def protect(parent, name, mode, is_directory=False):
        fd = os.open(name, flags | (os.O_DIRECTORY if is_directory else 0), dir_fd=parent)
        try:
            info = os.fstat(fd)
            expected_type = stat.S_ISDIR if is_directory else stat.S_ISREG
            if not expected_type(info.st_mode) or info.st_uid != os.geteuid():
                raise ValueError('Protected paths must have the deployment user as owner and the expected file type')
            if not is_directory and info.st_nlink != 1:
                raise ValueError('Protected files must not have hard links')
            if check:
                if stat.S_IMODE(info.st_mode) != mode:
                    raise ValueError('Deployment permissions differ from the required private modes')
            else:
                os.fchmod(fd, mode)
            return fd
        except BaseException:
            os.close(fd)
            raise

    root = protect(None, os.fspath(directory), 0o700, True)
    try:
        names = ('.deploy.lock',) if lock_only else ('.env', '.deploy.lock', 'docker-compose.override.yml')
        for name in names:
            if name != '.env' and name not in os.listdir(root):
                continue
            os.close(protect(root, name, 0o600))
        # Another lock holder can atomically replace release state or rename and
        # prune snapshots. Do not inspect those paths before acquiring the lock.
        if lock_only:
            return
        if not check:
            try:
                os.mkdir('backups', mode=0o700, dir_fd=root)
            except FileExistsError:
                pass
        backups = protect(root, 'backups', 0o700, True)
        try:
            def walk(parent):
                for name in os.listdir(parent):
                    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    is_directory = stat.S_ISDIR(info.st_mode)
                    fd = protect(parent, name, 0o700 if is_directory else 0o600, is_directory)
                    try:
                        if is_directory:
                            walk(fd)
                    finally:
                        os.close(fd)
            walk(backups)
        finally:
            os.close(backups)
    finally:
        os.close(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=Path.cwd())
    parser.add_argument('--check', action='store_true', help='Validate without changing permissions')
    parser.add_argument('--lock-only', action='store_true',
                        help='Protect only the deployment directory and lock path before acquiring the lock')
    args = parser.parse_args()
    secure(args.directory, args.check, args.lock_only)
    if args.lock_only:
        print('PASS: deployment directory and lock path are private')
    else:
        print('PASS: deployment directory, secrets and snapshots are private; app storage unchanged')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as error:
        print(f'ERROR: deployment permissions preflight failed: {error}', file=sys.stderr)
        sys.exit(1)
