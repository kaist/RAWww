## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Проверки потокового индекса Canon RAW Burst и виртуальных кадров."""

from __future__ import annotations

import struct
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import rawww.canon_burst as canon_burst
from rawww.canon_burst import (
    BurstFrame,
    CanonBurstError,
    materialize_frame,
    materialized_path,
    read_burst_index,
    read_frame_preview,
)


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


def _track(sample_sizes: list[int], sample_offsets: list[int]) -> bytes:
    hdlr = _box(b"hdlr", b"\0" * 8 + b"vide")
    stsz = _box(
        b"stsz",
        b"\0" * 8 + struct.pack(">I", len(sample_sizes))
        + b"".join(struct.pack(">I", size) for size in sample_sizes),
    )
    co64 = _box(
        b"co64",
        b"\0" * 4 + struct.pack(">I", len(sample_offsets))
        + b"".join(struct.pack(">Q", offset) for offset in sample_offsets),
    )
    return _box(b"trak", _box(b"mdia", hdlr + _box(b"minf", _box(b"stbl", stsz + co64))))


def _burst_bytes(samples: list[bytes]) -> bytes:
    ftyp = _box(b"ftyp", b"crx \0\0\0\0")
    placeholder = _box(b"moov", _track([len(value) for value in samples], [0] * len(samples)))
    first_offset = len(ftyp) + len(placeholder) + 8
    offsets = []
    position = first_offset
    for value in samples:
        offsets.append(position)
        position += len(value)
    moov = _box(b"moov", _track([len(value) for value in samples], offsets))
    return ftyp + moov + _box(b"mdat", b"".join(samples))


class CanonBurstTests(unittest.TestCase):
    def test_windows_retries_publish_when_temporary_file_is_locked(self) -> None:
        locked = PermissionError(13, "temporary file is locked")
        locked.winerror = 32
        operation = Mock(side_effect=[locked, locked, None])

        with (
            patch.object(canon_burst.sys, "platform", "win32"),
            patch.object(canon_burst, "sleep"),
        ):
            canon_burst._retry_windows_file_operation(operation)

        self.assertEqual(operation.call_count, 3)

    def test_non_windows_file_error_is_not_retried(self) -> None:
        locked = PermissionError(13, "temporary file is locked")
        locked.winerror = 32
        operation = Mock(side_effect=locked)

        with patch.object(canon_burst.sys, "platform", "linux"):
            with self.assertRaises(PermissionError):
                canon_burst._retry_windows_file_operation(operation)

        operation.assert_called_once_with()

    def test_regular_canon_cr3_is_not_mistaken_for_burst(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "metadata" / "canon_eos_r" / "IMG_0639.CR3"
        self.assertIsNone(read_burst_index(fixture))

    def test_indexes_and_reads_individual_preview(self) -> None:
        samples = [b"\xff\xd8first", b"\xff\xd8second"]
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "roll.cr3"
            path.write_bytes(_burst_bytes(samples))
            index = read_burst_index(path)
            self.assertIsNotNone(index)
            assert index is not None
            self.assertEqual(index.frame_count, 2)
            self.assertEqual(read_frame_preview(index, 1), samples[1])

    def test_single_frame_cr3_is_not_a_burst(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "single.cr3"
            path.write_bytes(_burst_bytes([b"\xff\xd8single"]))
            self.assertIsNone(read_burst_index(path))

    def test_changed_source_invalidates_index(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "roll.cr3"
            path.write_bytes(_burst_bytes([b"\xff\xd8one", b"\xff\xd8two"]))
            index = read_burst_index(path)
            assert index is not None
            with path.open("ab") as target:
                target.write(b"changed")
            with self.assertRaises(CanonBurstError):
                read_frame_preview(index, 0)

    def test_virtual_name_and_materialized_collision_are_safe(self) -> None:
        with TemporaryDirectory() as temporary:
            source = Path(temporary) / "roll.cr3"
            source.write_bytes(b"source")
            frame = BurstFrame(source, 2, 5)
            self.assertEqual(frame.name, "roll [003].CR3")
            self.assertEqual(frame.cache_name, "roll.cr3#burst-0002")
            target = materialized_path(frame)
            self.assertEqual(target.name, "roll_003.cr3")
            target.write_bytes(b"user data")
            with self.assertRaises(CanonBurstError):
                materialize_frame(frame)
            self.assertEqual(target.read_bytes(), b"user data")


if __name__ == "__main__":
    unittest.main()
