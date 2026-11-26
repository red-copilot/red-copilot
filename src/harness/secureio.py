"""Small no-follow helpers for files written by untrusted Agent processes."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import IO, Literal, overload

_DEFAULT_READ_LIMIT = 64 * 1024 * 1024


@overload
def open_regular_file(
    path: Path,
    mode: Literal["r", "a", "w"],
    *,
    encoding: str | None = "utf-8",
    errors: str = "strict",
) -> IO[str]: ...


@overload
def open_regular_file(
    path: Path,
    mode: Literal["rb", "ab", "wb"],
    *,
    encoding: str | None = "utf-8",
    errors: str = "strict",
) -> IO[bytes]: ...


def open_regular_file(
    path: Path,
    mode: str,
    *,
    encoding: str | None = "utf-8",
    errors: str = "strict",
) -> IO[str] | IO[bytes]:
    if not hasattr(os, "O_NOFOLLOW"):
        raise OSError("platform does not support no-follow Agent file access")
    flags = {
        "r": os.O_RDONLY,
        "a": os.O_WRONLY | os.O_APPEND | os.O_CREAT,
        "w": os.O_WRONLY | os.O_TRUNC | os.O_CREAT,
        "rb": os.O_RDONLY,
        "ab": os.O_WRONLY | os.O_APPEND | os.O_CREAT,
        "wb": os.O_WRONLY | os.O_TRUNC | os.O_CREAT,
    }[mode]
    flags |= os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    if mode in {"r", "rb"}:
        flags |= getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(f"expected a regular file: {path}")
        if "b" in mode:
            return os.fdopen(descriptor, mode)
        return os.fdopen(descriptor, mode, encoding=encoding, errors=errors)
    except BaseException:
        os.close(descriptor)
        raise


def read_regular_text(
    path: Path,
    *,
    encoding: str = "utf-8",
    errors: str = "replace",
    max_bytes: int = _DEFAULT_READ_LIMIT,
) -> str:
    with open_regular_file(path, "rb") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise OSError(f"Agent output exceeds the {max_bytes}-byte read limit: {path}")
    return data.decode(encoding, errors=errors)


def read_regular_text_prefix(
    path: Path,
    *,
    max_bytes: int,
    encoding: str = "utf-8",
    errors: str = "replace",
) -> str:
    """Bounded prefix read of an untrusted regular file; oversize truncates.

    Unlike ``read_regular_text`` (which rejects oversize input), this keeps
    the first ``max_bytes`` bytes and drops the rest — for callers whose
    contract is "truncate to a readable bound", not "refuse the artifact".
    """
    with open_regular_file(path, "rb") as handle:
        data = handle.read(max_bytes)
    return data.decode(encoding, errors=errors)


def poll_regular_chunk(
    path: Path,
    offset: int,
    *,
    chunk_bytes: int,
    identity: tuple[int, int] | None = None,
) -> tuple[bytes, int, tuple[int, int]]:
    """Read one bounded chunk of an append-only untrusted file for a cursor.

    Returns ``(chunk, new_offset, file_identity)`` where ``file_identity`` is
    the ``(st_mtime_ns, st_size)`` observed at this read; callers pass the
    previous identity back as ``identity``. The cursor may be stale — the
    writer's own file can be truncated or rewritten in place:

    - a shrunk file leaves the offset past every future append;
    - an equal-size file with a new mtime was rewritten in place;
    - a grown file whose byte before ``offset`` is no longer the newline the
      previous stream ended with was rewritten to a different length.

    All three restart the cursor from 0 within this same call — returning an
    empty chunk there would make the caller treat the reset as "no data" and
    hide the append. A genuine append keeps the newline boundary and the
    size-growth shape, so it never triggers a spurious reset.
    """
    with open_regular_file(path, "rb") as handle:
        info = os.fstat(handle.fileno())
        current = (info.st_mtime_ns, info.st_size)
        effective = offset
        if identity is not None and identity != current:
            stale = info.st_size < offset
            if not stale and info.st_size == identity[1]:
                stale = True  # same size, different mtime: rewritten in place
            elif not stale and info.st_mtime_ns != identity[0] and offset > 0:
                handle.seek(offset - 1)
                stale = handle.read(1) != b"\n"
                handle.seek(0)
            if stale:
                effective = 0
        handle.seek(effective)
        data = handle.read(chunk_bytes)
        return data, effective + len(data), current


def open_beneath(
    root: Path,
    relative_path: Path,
    mode: str,
    *,
    create_parents: bool = False,
) -> IO[str]:
    """Open an Agent workspace file without following any path component symlink."""
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise OSError("platform does not support safe workspace path traversal")
    if relative_path.is_absolute() or not relative_path.parts:
        raise OSError("workspace path must be relative and name a file")
    parts = relative_path.parts
    if any(part in {".", ".."} for part in parts):
        raise OSError("workspace path traversal is not allowed")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    directory_fd = os.open(root, directory_flags)
    try:
        for part in parts[:-1]:
            try:
                child_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            except FileNotFoundError:
                if not create_parents:
                    raise
                os.mkdir(part, mode=0o700, dir_fd=directory_fd)
                child_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child_fd

        flags_by_mode = {
            "r": os.O_RDONLY,
            "rb": os.O_RDONLY,
            "a": os.O_WRONLY | os.O_APPEND | os.O_CREAT,
            "ab": os.O_WRONLY | os.O_APPEND | os.O_CREAT,
            "w": os.O_WRONLY | os.O_TRUNC | os.O_CREAT,
            "wb": os.O_WRONLY | os.O_TRUNC | os.O_CREAT,
        }
        flags = flags_by_mode[mode] | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        if mode in {"r", "rb"}:
            flags |= getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(parts[-1], flags, 0o600, dir_fd=directory_fd)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError(f"expected a regular workspace file: {relative_path}")
            if "b" in mode:
                return os.fdopen(descriptor, mode)
            return os.fdopen(descriptor, mode, encoding="utf-8")
        except BaseException:
            os.close(descriptor)
            raise
    finally:
        os.close(directory_fd)
