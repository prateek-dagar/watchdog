""":module: watchdog.utils.dirsnapshot
:synopsis: Directory snapshots and comparison.
:author: yesudeep@google.com (Yesudeep Mangalapilly)
:author: Mickaël Schoentgen <contact@tiger-222.fr>

.. ADMONITION:: Where are the moved events? They "disappeared"

    This implementation does not take partition boundaries
    into consideration. It will only work when the directory
    tree is entirely on the same file system. More specifically,
    any part of the code that depends on inode numbers can
    break if partition boundaries are crossed. In these cases,
    the snapshot diff will represent file/directory movement as
    created and deleted events.

Classes
-------
.. autoclass:: DirectorySnapshot
   :members:
   :show-inheritance:

.. autoclass:: DirectorySnapshotDiff
   :members:
   :show-inheritance:

.. autoclass:: EmptyDirectorySnapshot
   :members:
   :show-inheritance:

"""

from __future__ import annotations

import contextlib
import errno
import os
import sys
from stat import S_ISDIR
from typing import TYPE_CHECKING

from watchdog.utils import platform
from watchdog.utils.statx import get_statx_birthtime_ns, is_statx_available

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from typing import Any

# C-level os.stat_result descriptors (constant for a given Python build/OS).
_HAS_ST_FLAGS = hasattr(os.stat_result, "st_flags")
_HAS_ST_FILE_ATTRIBUTES = hasattr(os.stat_result, "st_file_attributes")


def _partition_paths(
    paths: set[bytes | str],
    stat_map: dict[bytes | str, os.stat_result],
) -> tuple[list[bytes | str], list[bytes | str]]:
    """Split a set of paths into (dirs, files) in a single pass."""
    dirs: list[bytes | str] = []
    files: list[bytes | str] = []
    for path in paths:
        (dirs if S_ISDIR(stat_map[path].st_mode) else files).append(path)
    return dirs, files


class DirectorySnapshotDiff:
    """Compares two directory snapshots and creates an object that represents
    the difference between the two snapshots.

    :param ref:
        The reference directory snapshot.
    :type ref:
        :class:`DirectorySnapshot`
    :param snapshot:
        The directory snapshot which will be compared
        with the reference snapshot.
    :type snapshot:
        :class:`DirectorySnapshot`
    :param ignore_device:
        A boolean indicating whether to ignore the device id or not.
        By default, a file may be uniquely identified by a combination of its first
        inode and its device id. The problem is that the device id may (or may not)
        change between system boots. This problem would cause the DirectorySnapshotDiff
        to think a file has been deleted and created again but it would be the
        exact same file.
        Set to True only if you are sure you will always use the same device.
    :type ignore_device:
        :class:`bool`
    """

    def __init__(
        self,
        ref: DirectorySnapshot,
        snapshot: DirectorySnapshot,
        *,
        ignore_device: bool = False,
    ) -> None:
        ref_stat = ref._stat_info  # noqa: SLF001
        snap_stat = snapshot._stat_info  # noqa: SLF001
        ref_keys = ref_stat.keys()
        snap_keys = snap_stat.keys()

        created: set[bytes | str] = set(snap_keys - ref_keys)
        deleted: set[bytes | str] = set(ref_keys - snap_keys)
        common = ref_keys & snap_keys

        use_fast_inode = type(ref).inode is _DEFAULT_SNAPSHOT_INODE and type(snapshot).inode is _DEFAULT_SNAPSHOT_INODE
        is_windows = platform.is_windows()
        is_freebsd = sys.platform.startswith("freebsd")
        use_fast_btime = (
            not is_windows
            and type(ref).birthtime in (_DEFAULT_SNAPSHOT_BIRTHTIME, EmptyDirectorySnapshot.birthtime)
            and type(snapshot).birthtime in (_DEFAULT_SNAPSHOT_BIRTHTIME, EmptyDirectorySnapshot.birthtime)
        )
        ref_btimes: dict[bytes | str, int | None] | None = getattr(ref, "_birthtimes", None)
        snap_btimes: dict[bytes | str, int | None] | None = getattr(snapshot, "_birthtimes", None)

        if ignore_device:
            # Retain each snapshot's canonical path when hard links share an inode.
            if use_fast_inode:
                ref_i2p = getattr(ref, "_inode_to_path", None)
                snap_i2p = getattr(snapshot, "_inode_to_path", None)
                ref_paths = (
                    {st.st_ino: ref_i2p.get((st.st_ino, st.st_dev)) for st in ref_stat.values()} if ref_i2p else {}
                )
                snapshot_paths = (
                    {st.st_ino: snap_i2p.get((st.st_ino, st.st_dev)) for st in snap_stat.values()} if snap_i2p else {}
                )
            else:
                ref_paths = {ref.inode(path)[0]: ref.path(ref.inode(path)) for path in ref_keys}
                snapshot_paths = {snapshot.inode(path)[0]: snapshot.path(snapshot.inode(path)) for path in snap_keys}

            def get_ref_path(uid: tuple[int, int]) -> bytes | str | None:
                return ref.path(uid) or ref_paths.get(uid[0])

            def get_snapshot_path(uid: tuple[int, int]) -> bytes | str | None:
                return snapshot.path(uid) or snapshot_paths.get(uid[0])
        else:
            get_ref_path = ref.path
            get_snapshot_path = snapshot.path

        def get_btime(
            snap: DirectorySnapshot,
            btimes: dict[bytes | str, int | None] | None,
            path: bytes | str,
        ) -> int | None:
            if is_windows:
                return None
            if use_fast_btime and btimes is not None and (bt := btimes.get(path, ...)) is not ...:
                return bt
            return snap.birthtime(path)

        def is_same_entry(
            ref_path: bytes | str,
            new_path: bytes | str,
            ref_st: os.stat_result,
            snap_st: os.stat_result,
        ) -> bool:
            # A file cannot be renamed into a directory or vice versa (#303).
            if S_ISDIR(ref_st.st_mode) != S_ISDIR(snap_st.st_mode):
                return False

            if is_windows:
                return True

            # Compare available birth times to detect recycled inodes (#992).
            ref_btime = get_btime(ref, ref_btimes, ref_path)
            new_btime = get_btime(snapshot, snap_btimes, new_path)
            if ref_btime is not None and new_btime is not None:
                # FreeBSD utimes(2) can lower birth time when mtime is backdated.
                if ref_path == new_path and is_freebsd and new_btime < ref_btime:
                    return True
                return ref_btime == new_btime

            return True

        # Single pass over paths present in both snapshots:
        # detect same-path replacements AND modifications/attribute changes.
        modified: set[bytes | str] = set()
        attrib: set[bytes | str] = set()
        for path in common:
            ref_st = ref_stat[path]
            snap_st = snap_stat[path]
            if use_fast_inode:
                same_inode = ref_st.st_ino == snap_st.st_ino and (ignore_device or ref_st.st_dev == snap_st.st_dev)
            elif ignore_device:
                same_inode = ref.inode(path)[0] == snapshot.inode(path)[0]
            else:
                same_inode = ref.inode(path) == snapshot.inode(path)

            if not same_inode or not is_same_entry(path, path, ref_st, snap_st):
                created.add(path)
                deleted.add(path)
            else:
                if ref_st.st_mtime != snap_st.st_mtime or ref_st.st_size != snap_st.st_size:
                    modified.add(path)
                if self.is_attrib_modified(ref_st, snap_st):
                    attrib.add(path)

        # Find moved paths.
        moved: set[tuple[bytes | str, bytes | str]] = set()
        for path in list(deleted):
            ref_st = ref_stat[path]
            inode = (ref_st.st_ino, ref_st.st_dev) if use_fast_inode else ref.inode(path)
            new_path = get_snapshot_path(inode)
            # Only treat this as a move if the inode is now reachable through a
            # genuinely new path. A symlink (or hard link) shares its target's
            # inode, so ``new_path`` may still point at the unchanged target,
            # which is not a move.
            if new_path is not None:
                ref_new_st = ref_stat.get(new_path)
                if (
                    ref_new_st is None
                    or ((ref_new_st.st_ino, ref_new_st.st_dev) if use_fast_inode else ref.inode(new_path)) != inode
                ) and is_same_entry(path, new_path, ref_st, snap_stat[new_path]):
                    deleted.remove(path)
                    moved.add((path, new_path))

        for path in list(created):
            snap_st = snap_stat[path]
            inode = (snap_st.st_ino, snap_st.st_dev) if use_fast_inode else snapshot.inode(path)
            old_path = get_ref_path(inode)
            # Only treat this as a move if the inode's previous path is really
            # gone. A symlink (or hard link) shares its target's inode, so
            # ``old_path`` may still exist with that inode, which means this is a
            # creation, not a move.
            if old_path is not None:
                snap_old_st = snap_stat.get(old_path)
                if (
                    snap_old_st is None
                    or ((snap_old_st.st_ino, snap_old_st.st_dev) if use_fast_inode else snapshot.inode(old_path))
                    != inode
                ) and is_same_entry(old_path, path, ref_stat[old_path], snap_st):
                    created.remove(path)
                    moved.add((old_path, path))

        # A hard link can make the two inode lookups select different paths.
        # A move found in either pass must not also remain a create or delete.
        for old_path, new_path in moved:
            deleted.discard(old_path)
            created.discard(new_path)
            ref_st = ref_stat[old_path]
            snap_st = snap_stat[new_path]
            if ref_st.st_mtime != snap_st.st_mtime or ref_st.st_size != snap_st.st_size:
                modified.add(old_path)
            if self.is_attrib_modified(ref_st, snap_st):
                attrib.add(old_path)

        self._dirs_created, self._files_created = _partition_paths(created, snap_stat)
        self._dirs_deleted, self._files_deleted = _partition_paths(deleted, ref_stat)
        self._dirs_modified, self._files_modified = _partition_paths(modified, ref_stat)
        self._dirs_attrib, self._files_attrib = _partition_paths(attrib, ref_stat)

        dirs_moved: list[tuple[bytes | str, bytes | str]] = []
        files_moved: list[tuple[bytes | str, bytes | str]] = []
        for item in moved:
            (dirs_moved if S_ISDIR(ref_stat[item[0]].st_mode) else files_moved).append(item)
        self._dirs_moved = dirs_moved
        self._files_moved = files_moved

    @staticmethod
    def is_attrib_modified(ref_stat: os.stat_result, snap_stat: os.stat_result) -> bool:
        """Determines whether permissions, file attributes, or ownership have changed."""
        if type(ref_stat) is os.stat_result and type(snap_stat) is os.stat_result:
            if (
                ref_stat.st_mode != snap_stat.st_mode
                or ref_stat.st_uid != snap_stat.st_uid
                or ref_stat.st_gid != snap_stat.st_gid
            ):
                return True
            if _HAS_ST_FLAGS:
                ref_any: Any = ref_stat
                snap_any: Any = snap_stat
                if ref_any.st_flags != snap_any.st_flags:
                    return True
            if _HAS_ST_FILE_ATTRIBUTES:
                ref_any = ref_stat
                snap_any = snap_stat
                return bool(ref_any.st_file_attributes != snap_any.st_file_attributes)
            return False
        return (
            # File mode and permission bits (e.g. chmod, read/write/execute bits)
            getattr(ref_stat, "st_mode", None) != getattr(snap_stat, "st_mode", None)
            # Windows file attributes bitmask (e.g. attrib +h, readonly, system, archive)
            or getattr(ref_stat, "st_file_attributes", None) != getattr(snap_stat, "st_file_attributes", None)
            # Owner user ID (e.g. chown)
            or getattr(ref_stat, "st_uid", None) != getattr(snap_stat, "st_uid", None)
            # Owner group ID (e.g. chown, chgrp)
            or getattr(ref_stat, "st_gid", None) != getattr(snap_stat, "st_gid", None)
            # macOS & BSD file flags (e.g. chflags immutable, hidden, append)
            or getattr(ref_stat, "st_flags", None) != getattr(snap_stat, "st_flags", None)
        )

    def __str__(self) -> str:
        return self.__repr__()

    def __repr__(self) -> str:
        fmt = (
            "<{0} files(created={1}, deleted={2}, modified={3}, moved={4}, attrib={5}),"
            " folders(created={6}, deleted={7}, modified={8}, moved={9}, attrib={10})>"
        )
        return fmt.format(
            type(self).__name__,
            len(self._files_created),
            len(self._files_deleted),
            len(self._files_modified),
            len(self._files_moved),
            len(self._files_attrib),
            len(self._dirs_created),
            len(self._dirs_deleted),
            len(self._dirs_modified),
            len(self._dirs_moved),
            len(self._dirs_attrib),
        )

    def __len__(self) -> int:
        return (
            len(self._files_created)
            + len(self._files_deleted)
            + len(self._files_modified)
            + len(self._files_moved)
            + len(self._files_attrib)
            + len(self._dirs_created)
            + len(self._dirs_deleted)
            + len(self._dirs_modified)
            + len(self._dirs_moved)
            + len(self._dirs_attrib)
        )

    @property
    def files_created(self) -> list[bytes | str]:
        """List of files that were created."""
        return self._files_created

    @property
    def files_deleted(self) -> list[bytes | str]:
        """List of files that were deleted."""
        return self._files_deleted

    @property
    def files_modified(self) -> list[bytes | str]:
        """List of files that were modified."""
        return self._files_modified

    @property
    def files_moved(self) -> list[tuple[bytes | str, bytes | str]]:
        """List of files that were moved.

        Each event is a two-tuple the first item of which is the path
        that has been renamed to the second item in the tuple.
        """
        return self._files_moved

    @property
    def files_attrib(self) -> list[bytes | str]:
        """List of files whose attributes or permissions were modified."""
        return self._files_attrib

    @property
    def dirs_modified(self) -> list[bytes | str]:
        """List of directories that were modified."""
        return self._dirs_modified

    @property
    def dirs_moved(self) -> list[tuple[bytes | str, bytes | str]]:
        """List of directories that were moved.

        Each event is a two-tuple the first item of which is the path
        that has been renamed to the second item in the tuple.
        """
        return self._dirs_moved

    @property
    def dirs_deleted(self) -> list[bytes | str]:
        """List of directories that were deleted."""
        return self._dirs_deleted

    @property
    def dirs_created(self) -> list[bytes | str]:
        """List of directories that were created."""
        return self._dirs_created

    @property
    def dirs_attrib(self) -> list[bytes | str]:
        """List of directories whose attributes or permissions were modified."""
        return self._dirs_attrib

    class ContextManager:
        """Context manager that creates two directory snapshots and a
        diff object that represents the difference between the two snapshots.

        :param path:
            The directory path for which a snapshot should be taken.
        :type path:
            ``str``
        :param recursive:
            ``True`` if the entire directory tree should be included in the
            snapshot; ``False`` otherwise.
        :type recursive:
            ``bool``
        :param stat:
            Use custom stat function that returns a stat structure for path.
            Currently only st_dev, st_ino, st_mode and st_mtime are needed.

            A function taking a ``path`` as argument which will be called
            for every entry in the directory tree.
        :param listdir:
            Use custom listdir function. For details see ``os.scandir``.
        :param ignore_device:
            A boolean indicating whether to ignore the device id or not.
            By default, a file may be uniquely identified by a combination of its first
            inode and its device id. The problem is that the device id may (or may not)
            change between system boots. This problem would cause the DirectorySnapshotDiff
            to think a file has been deleted and created again but it would be the
            exact same file.
            Set to True only if you are sure you will always use the same device.
        :type ignore_device:
            :class:`bool`
        """

        def __init__(
            self,
            path: str,
            *,
            recursive: bool = True,
            stat: Callable[[str], os.stat_result] = os.stat,
            listdir: Callable[[str | None], Iterator[os.DirEntry]] = os.scandir,
            ignore_device: bool = False,
        ) -> None:
            self.path = path
            self.recursive = recursive
            self.stat = stat
            self.listdir = listdir
            self.ignore_device = ignore_device

        def __enter__(self) -> None:
            self.pre_snapshot = self.get_snapshot()

        def __exit__(self, *args: object) -> None:
            self.post_snapshot = self.get_snapshot()
            self.diff = DirectorySnapshotDiff(
                self.pre_snapshot,
                self.post_snapshot,
                ignore_device=self.ignore_device,
            )

        def get_snapshot(self) -> DirectorySnapshot:
            return DirectorySnapshot(
                path=self.path,
                recursive=self.recursive,
                stat=self.stat,
                listdir=self.listdir,
            )


class DirectorySnapshot:
    """A snapshot of stat information of files in a directory.

    :param path:
        The directory path for which a snapshot should be taken.
    :type path:
        ``str``
    :param recursive:
        ``True`` if the entire directory tree should be included in the
        snapshot; ``False`` otherwise.
    :type recursive:
        ``bool``
    :param stat:
        Use custom stat function that returns a stat structure for path.
        Currently only st_dev, st_ino, st_mode and st_mtime are needed.

        A function taking a ``path`` as argument which will be called
        for every entry in the directory tree.
    :param listdir:
        Use custom listdir function. For details see ``os.scandir``.
    """

    def __init__(
        self,
        path: str,
        *,
        recursive: bool = True,
        stat: Callable[[str], os.stat_result] = os.stat,
        listdir: Callable[[str | None], Iterator[os.DirEntry]] = os.scandir,
    ) -> None:
        self.recursive = recursive
        self.stat = stat
        self.listdir = listdir

        self._stat_info: dict[bytes | str, os.stat_result] = {}
        self._inode_to_path: dict[tuple[int, int], bytes | str] = {}
        self._birthtimes: dict[bytes | str, int | None] = {}

        st = self.stat(path)
        self._stat_info[path] = st
        self._inode_to_path[(st.st_ino, st.st_dev)] = path
        # Linux statx reads the live path, which may no longer exist at diff time.
        self.birthtime(path)

        for p, st in self.walk(path):
            i = (st.st_ino, st.st_dev)
            self._inode_to_path[i] = p
            self._stat_info[p] = st
            self.birthtime(p)

    def walk(self, root: str) -> Iterator[tuple[str, os.stat_result]]:
        try:
            paths = [os.path.join(root, entry.name) for entry in self.listdir(root)]
        except OSError as e:
            # Directory may have been deleted between finding it in the directory
            # list of its parent and trying to delete its contents. If this
            # happens we treat it as empty. Likewise if the directory was replaced
            # with a file of the same name (less likely, but possible).
            if e.errno in (errno.ENOENT, errno.ENOTDIR, errno.EINVAL):
                return
            else:
                raise

        entries = []
        for p in paths:
            with contextlib.suppress(OSError):
                entry = (p, self.stat(p))
                entries.append(entry)
                yield entry

        if self.recursive:
            for path, st in entries:
                with contextlib.suppress(PermissionError):
                    if S_ISDIR(st.st_mode):
                        yield from self.walk(path)

    @property
    def paths(self) -> set[bytes | str]:
        """Set of file/directory paths in the snapshot."""
        return set(self._stat_info.keys())

    def path(self, uid: tuple[int, int]) -> bytes | str | None:
        """Returns path for id. None if id is unknown to this snapshot."""
        return self._inode_to_path.get(uid)

    def inode(self, path: bytes | str) -> tuple[int, int]:
        """Returns an id for path."""
        st = self._stat_info[path]
        return (st.st_ino, st.st_dev)

    def isdir(self, path: bytes | str) -> bool:
        """Returns ``True`` if the path is a directory."""
        return S_ISDIR(self._stat_info[path].st_mode)

    def mtime(self, path: bytes | str) -> float:
        """Returns the last modification time for path."""
        return self._stat_info[path].st_mtime

    def birthtime(self, path: bytes | str) -> int | None:
        """Returns the birth/creation time in nanoseconds for path, or None if unsupported.

        - macOS / BSD: uses ``st.st_birthtime_ns``
        - Linux: uses ``statx(2)`` ``stx_btime``, captured when taking the snapshot
          (see https://man7.org/linux/man-pages/man2/statx.2.html)
        """
        if platform.is_windows():
            return None

        # Preserve cached None and zero without querying the live path again.
        if (birthtime := self._birthtimes.get(path, ...)) is not ...:
            return birthtime

        if st := self._stat_info.get(path):
            # macOS / BSD provides st_birthtime_ns natively
            btime_ns: int | None = getattr(st, "st_birthtime_ns", None)
            if btime_ns is not None:
                self._birthtimes[path] = btime = int(btime_ns)
                return btime
            # Fallback to float st_birthtime if nanoseconds attribute is unavailable
            btime_sec: float | None = getattr(st, "st_birthtime", None)
            if btime_sec is not None:
                self._birthtimes[path] = btime = int(btime_sec * 1_000_000_000)
                return btime

        if platform.is_linux() and is_statx_available():
            follow = self.stat == os.stat
            btime_ns = get_statx_birthtime_ns(path, follow_symlinks=follow)
            self._birthtimes[path] = btime_ns
            return btime_ns

        self._birthtimes[path] = None
        return None

    def size(self, path: bytes | str) -> int:
        """Returns the size of the file identified by path."""
        return self._stat_info[path].st_size

    def mode(self, path: bytes | str) -> int:
        """Returns the mode/permissions for path."""
        return self._stat_info[path].st_mode

    def stat_info(self, path: bytes | str) -> os.stat_result:
        """Returns a stat information object for the specified path from
        the snapshot.

        Attached information is subject to change. Do not use unless
        you specify `stat` in constructor. Use :meth:`inode`, :meth:`mtime`,
        :meth:`isdir` instead.

        :param path:
            The path for which stat information should be obtained
            from a snapshot.
        """
        return self._stat_info[path]

    def __sub__(self, previous_dirsnap: DirectorySnapshot) -> DirectorySnapshotDiff:
        """Allow subtracting a DirectorySnapshot object instance from
        another.

        :returns:
            A :class:`DirectorySnapshotDiff` object.
        """
        return DirectorySnapshotDiff(previous_dirsnap, self)

    def __str__(self) -> str:
        return self.__repr__()

    def __repr__(self) -> str:
        return str(self._stat_info)


# Bound after class definition so DirectorySnapshotDiff detects both subclass overrides and unittest.mock.patch.object.
_DEFAULT_SNAPSHOT_INODE = DirectorySnapshot.inode
_DEFAULT_SNAPSHOT_BIRTHTIME = DirectorySnapshot.birthtime


class EmptyDirectorySnapshot(DirectorySnapshot):
    """Class to implement an empty snapshot. This is used together with
    DirectorySnapshot and DirectorySnapshotDiff in order to get all the files/folders
    in the directory as created.
    """

    def __init__(self) -> None:
        self._stat_info: dict[bytes | str, os.stat_result] = {}

    @staticmethod
    def path(_: Any) -> None:
        """Mock up method to return the path of the received inode. As the snapshot
        is intended to be empty, it always returns None.

        :returns:
            None.
        """
        return

    @property
    def paths(self) -> set:
        """Mock up method to return a set of file/directory paths in the snapshot. As
        the snapshot is intended to be empty, it always returns an empty set.

        :returns:
            An empty set.
        """
        return set()

    def birthtime(self, path: bytes | str) -> int | None:
        """Mock up method to return birth time. As the snapshot is intended to be
        empty, it always returns None.
        """
        return None
