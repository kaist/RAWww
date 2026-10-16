## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Проверки границ параллельного скачивания без сети и пользовательских файлов."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event, Lock, Thread
from types import SimpleNamespace
from unittest.mock import Mock

from rawww.yandex_cloud import download_files


class DownloadPoolTests(unittest.TestCase):
    """Проверяет параллелизм, суммарный прогресс и остановку после ошибок."""

    def test_eight_workers_and_total_progress(self):
        barrier = Barrier(8, timeout=5)
        lock = Lock()
        active = 0
        peak = 0
        reports = []

        def download(path, target, *, progress, checkpoint):
            nonlocal active, peak
            checkpoint()
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                barrier.wait()
                progress(42)
                target.write_bytes(b"photo")
            finally:
                with lock:
                    active -= 1

        with TemporaryDirectory() as directory:
            files = [(SimpleNamespace(path=str(i)), Path(directory) / str(i)) for i in range(24)]
            download_files(SimpleNamespace(download_file=download), files,
                           progress=lambda item, count, done: reports.append((count, done)))
            self.assertTrue(all(target.exists() for _, target in files))
        self.assertEqual(peak, 8)
        self.assertEqual(sum(count for count, _ in reports), 24 * 42)
        self.assertEqual(sum(done for _, done in reports), 24)
        self.assertEqual(active, 0)

    def test_failure_stops_workers_and_preserves_original_error(self):
        barrier = Barrier(8, timeout=5)
        finished = []
        failure = OSError("network failed")

        def download(path, target, *, progress, checkpoint):
            try:
                barrier.wait()
                if path == "0":
                    raise failure
                while True:
                    checkpoint()
            finally:
                finished.append(path)

        with TemporaryDirectory() as directory:
            files = [(SimpleNamespace(path=str(i)), Path(directory) / str(i)) for i in range(24)]
            with self.assertRaises(OSError) as caught:
                download_files(SimpleNamespace(download_file=download), files)
        self.assertIs(caught.exception, failure)
        self.assertEqual(len(finished), 8)

    def test_cancel_before_start_and_duplicate_targets(self):
        client = Mock()
        with TemporaryDirectory() as directory:
            entry = (SimpleNamespace(path="photo"), Path(directory) / "photo")
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                download_files(client, [entry], checkpoint=Mock(side_effect=RuntimeError("cancelled")))
            with self.assertRaises(FileExistsError):
                download_files(client, [entry, entry])
        client.download_file.assert_not_called()

    def test_pause_blocks_all_workers_until_resumed(self):
        reached = Barrier(9, timeout=5)
        resumed = Event()
        downloaded = []
        errors = []

        def checkpoint():
            if not resumed.is_set():
                reached.wait()
                if not resumed.wait(5):
                    raise TimeoutError("resume timed out")

        def run(files):
            try:
                download_files(Mock(download_file=lambda *args, **kwargs: downloaded.append(args[0])),
                               files, checkpoint=checkpoint)
            except Exception as exc:
                errors.append(exc)

        with TemporaryDirectory() as directory:
            files = [(SimpleNamespace(path=str(i)), Path(directory) / str(i)) for i in range(8)]
            thread = Thread(target=run, args=(files,))
            thread.start()
            try:
                reached.wait()
                self.assertEqual(downloaded, [])
            finally:
                resumed.set()
                thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(downloaded), 8)
