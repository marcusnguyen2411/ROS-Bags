#!/usr/bin/env python3
"""Download a completed rosbag directory from an SSH-accessible computer."""

import argparse
import ctypes
import errno
import fcntl
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys


class TransferError(Exception):
    """A problem the user can act on without a Python traceback."""


def parse_source(value):
    """Return a normalized rsync source and its remote directory name."""
    host, separator, path = value.partition(":")
    if not separator or not path.startswith("/"):
        raise TransferError("Source must look like user@host:/absolute/path/to/bag")

    # Accept SSH host aliases, DNS names, and IPv4 addresses. IPv6 literals
    # can be given an alias in ~/.ssh/config.
    host_pattern = r"(?:[A-Za-z0-9_][A-Za-z0-9_.+\-]*@)?[A-Za-z0-9][A-Za-z0-9_.\-]*"
    if not re.fullmatch(host_pattern, host):
        raise TransferError("Invalid SSH host; use a hostname, IPv4 address, or SSH alias")
    if any(ord(character) < 32 or ord(character) == 127 for character in path):
        raise TransferError("The remote path cannot contain control characters")
    if any(part in (".", "..") for part in path.split("/")):
        raise TransferError("Use an absolute path without . or .. components")

    path = path.rstrip("/")
    name = PurePosixPath(path).name
    if not path or not name:
        raise TransferError("The source must name a bag directory, not the remote root")
    return f"{host}:{path}", name


def rename_without_replacing(source, destination):
    """Publish atomically on Linux, even if another process creates destination."""
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = libc.renameat2
    except AttributeError as exc:
        raise TransferError("This system does not support a safe, no-overwrite rename") from exc
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    # AT_FDCWD and RENAME_NOREPLACE are Linux constants. Both paths live in
    # the destination folder, so this is always within the same filesystem.
    if renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        error = ctypes.get_errno()
        if error in (errno.EEXIST, errno.ENOTEMPTY):
            raise TransferError(f"Destination already exists: {destination}")
        raise TransferError(
            f"Could not safely publish the completed bag: {os.strerror(error)}. "
            "The downloaded files are still in the partial directory."
        )


def check_directory(path, description):
    if path.is_symlink() or not path.is_dir():
        raise TransferError(f"{description} is not a regular directory: {path}")


def fetch(source, destination):
    source, bag_name = parse_source(source)
    for program in ("rsync", "ssh"):
        if not shutil.which(program):
            raise TransferError(f"{program} is required; install it on the laptop")

    destination = Path(destination).expanduser()
    destination.mkdir(parents=True, exist_ok=True)
    destination = destination.resolve()
    final = destination / bag_name
    partial = destination / f".{bag_name}.partial"

    if os.path.lexists(final):
        raise TransferError(f"Destination already exists; nothing was overwritten: {final}")

    try:
        partial.mkdir()
    except FileExistsError:
        check_directory(partial, "Partial transfer path")

    lock_path = partial / ".lock"
    # Never follow a pre-existing link for the lock or the source marker.
    lock_flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        lock_fd = os.open(lock_path, lock_flags, 0o600)
    except OSError as exc:
        raise TransferError(f"Cannot open the partial transfer lock: {exc}") from exc

    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise TransferError(f"Another transfer is using {partial}") from exc

        if os.path.lexists(final):
            raise TransferError(f"Destination already exists; nothing was overwritten: {final}")
        marker = partial / ".source"
        if os.path.lexists(marker):
            if marker.is_symlink() or not marker.is_file():
                raise TransferError(f"Invalid partial-transfer source marker: {marker}")
            if marker.read_text(encoding="utf-8") != source:
                raise TransferError(
                    f"The partial directory belongs to a different source: {partial}. "
                    "Choose another destination or move that partial directory first."
                )
        else:
            if any(entry.name != ".lock" for entry in partial.iterdir()):
                raise TransferError(f"Partial directory is not empty and has no source marker: {partial}")
            with marker.open("x", encoding="utf-8") as marker_file:
                marker_file.write(source)

        data = partial / "bag"
        try:
            data.mkdir()
        except FileExistsError:
            check_directory(data, "Partial bag path")

        command = [
            "rsync", "--recursive", "--times", "--links", "--partial",
            "--info=progress2", "--protect-args", "--",
            f"{source}/", f"{data}/",
        ]
        print(f"Copying {source} to {final}", flush=True)
        print("If interrupted, run the same command again to resume.", flush=True)
        result = subprocess.run(command, check=False)
        if result.returncode:
            raise TransferError(
                f"rsync exited with code {result.returncode}. "
                f"Partial files remain in {partial}; rerun the same command to resume."
            )

        metadata = data / "metadata.yaml"
        if metadata.is_symlink() or not metadata.is_file():
            raise TransferError(
                f"No regular metadata.yaml was copied. Check the source and wait for recording to finish. "
                f"Partial files remain in {partial}."
            )

        rename_without_replacing(data, final)
        # Only remove files the tool created. If anything else is present,
        # leave the partial directory in place rather than deleting user data.
        marker.unlink()
        lock_path.unlink()
        try:
            partial.rmdir()
        except OSError:
            pass
        print(f"Finished: {final}")
        print("metadata.yaml is present; use 'ros2 bag info' to check the bag on a ROS 2 system.")
    finally:
        os.close(lock_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Download one completed ROS 2 bag from a computer reachable over SSH."
    )
    parser.add_argument("source", help="user@host:/absolute/path/to/bag_directory")
    parser.add_argument(
        "--destination", "-d", default="~/rosbags",
        help="local parent folder (default: ~/rosbags)",
    )
    arguments = parser.parse_args(argv)
    try:
        fetch(arguments.source, arguments.destination)
    except (TransferError, OSError, UnicodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted. Run the same command again to resume.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
