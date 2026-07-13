import os
import unittest
from concurrent.futures import Future
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QImage

from rawww.decode_cache import DecodeCache
from rawww.decode_scheduler import DecodeScheduler
from rawww.imaging import DecodedImage

THUMB_SIZE = 256
ORIGINAL_SIZE = 0


def _decoded(name: str) -> DecodedImage:
    image = QImage(4, 4, QImage.Format.Format_RGBA8888)
    return DecodedImage(path=Path(name), image=image, width=4, height=4)


class _Signal:
    def __init__(self) -> None:
        self.emitted: list[tuple] = []

    def emit(self, *args: object) -> None:
        self.emitted.append(args)


class _Bridge:
    def __init__(self) -> None:
        self.decoded = _Signal()
        self.failed = _Signal()


class _FolderCache:
    def __init__(self, loads: dict | None = None, raises: bool = False) -> None:
        self._loads = loads or {}
        self._raises = raises
        self.stored: list = []

    def load(self, path: Path, size: int):
        if self._raises:
            raise RuntimeError("boom")
        return self._loads.get((path, size))

    def store_pixels(self, result, size: int) -> None:
        self.stored.append((result, size))


class _VideoThumbnailer:
    def __init__(self) -> None:
        self.requested: list[Path] = []

    def request(self, path: Path) -> None:
        self.requested.append(path)


class _SyncExecutor:
    """Runs work immediately so ``add_done_callback`` fires synchronously."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.shut = False

    def submit(self, fn, *args):
        self.calls.append(args)
        future: Future = Future()
        try:
            future.set_result(fn(*args))
        except Exception as exc:  # noqa: BLE001 - mirror executor behaviour
            future.set_exception(exc)
        return future

    def shutdown(self, **_: object) -> None:
        self.shut = True


class _PendingExecutor:
    """Returns an unresolved future so completion callbacks never run."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.shut = False

    def submit(self, fn, *args):
        self.calls.append((fn, args))
        return Future()

    def shutdown(self, **_: object) -> None:
        self.shut = True


class _Host:
    def __init__(self, folder_cache: _FolderCache | None) -> None:
        self.closing = False
        self.current_dir = Path("/photos")
        self.current_path: Path | None = None
        self.workspace_active = True
        self.folder_cache = folder_cache
        self.decode_cache = DecodeCache(
            ram_limit=96,
            full_limit=21,
            thumbnail_bytes_limit=700 * 1024 * 1024,
            original_size=ORIGINAL_SIZE,
            thumb_size=THUMB_SIZE,
        )
        self.bridge = _Bridge()
        self.video_thumbnailer = _VideoThumbnailer()


def _make(folder_cache: _FolderCache | None = None) -> tuple[DecodeScheduler, _Host]:
    host = _Host(folder_cache if folder_cache is not None else _FolderCache())
    scheduler = DecodeScheduler(
        host,
        thumb_size=THUMB_SIZE,
        original_size=ORIGINAL_SIZE,
        current_workers=1,
        background_workers=1,
        visible_thumb_workers=1,
        visible_thumb_lookup_workers=1,
    )
    # Replace the real thread pools with synchronous ones by default.
    scheduler.background_cache_lookup_executor = _SyncExecutor()
    scheduler.visible_thumb_cache_lookup_executor = _SyncExecutor()
    return scheduler, host


class DecodeSchedulerTests(unittest.TestCase):
    def test_cache_hit_emits_without_scheduling(self) -> None:
        scheduler, host = _make()
        path = host.current_dir / "a.jpg"
        decoded = _decoded(str(path))
        host.decode_cache.put((path, THUMB_SIZE), decoded)
        scheduler.submit_decode(path, THUMB_SIZE, full_priority=False)
        self.assertEqual(host.bridge.decoded.emitted, [((decoded, THUMB_SIZE),)])
        self.assertEqual(scheduler.pending, {})
        self.assertEqual(scheduler.background_cache_lookup_executor.calls, [])

    def test_duplicate_key_is_suppressed(self) -> None:
        scheduler, host = _make()
        path = host.current_dir / "a.jpg"
        scheduler.pending[(path, THUMB_SIZE)] = Future()
        scheduler.submit_decode(path, THUMB_SIZE, full_priority=False)
        self.assertEqual(scheduler.background_cache_lookup_executor.calls, [])
        self.assertEqual(host.bridge.decoded.emitted, [])

    def test_missing_folder_cache_does_nothing(self) -> None:
        scheduler, host = _make()
        host.folder_cache = None
        path = host.current_dir / "a.jpg"
        scheduler.submit_decode(path, THUMB_SIZE, full_priority=False)
        self.assertEqual(scheduler.pending, {})
        self.assertEqual(host.bridge.decoded.emitted, [])

    def test_thumbnail_cache_lookup_hit(self) -> None:
        path = Path("/photos/a.jpg")
        decoded = _decoded(str(path))
        folder = _FolderCache(loads={(path, THUMB_SIZE): decoded})
        scheduler, host = _make(folder)
        scheduler.submit_decode(path, THUMB_SIZE, full_priority=False)
        self.assertEqual(host.bridge.decoded.emitted, [((decoded, THUMB_SIZE),)])
        self.assertIs(host.decode_cache.get((path, THUMB_SIZE)), decoded)
        self.assertEqual(scheduler.pending, {})

    def test_visible_priority_uses_visible_executor(self) -> None:
        path = Path("/photos/a.jpg")
        # load returns None so the key stays pending until the (unused) process pool.
        folder = _FolderCache(loads={})
        scheduler, host = _make(folder)
        scheduler.visible_thumb_decode_executor = _PendingExecutor()
        scheduler.submit_decode(path, THUMB_SIZE, full_priority=False, visible_priority=True)
        self.assertEqual(len(scheduler.visible_thumb_cache_lookup_executor.calls), 1)
        self.assertEqual(scheduler.background_cache_lookup_executor.calls, [])
        # Cache miss falls back to the visible-thumb decode pool.
        self.assertEqual(len(scheduler.visible_thumb_decode_executor.calls), 1)

    def test_stale_directory_result_is_rejected(self) -> None:
        path = Path("/other/a.jpg")  # parent != host.current_dir
        decoded = _decoded(str(path))
        folder = _FolderCache(loads={(path, THUMB_SIZE): decoded})
        scheduler, host = _make(folder)
        scheduler.submit_decode(path, THUMB_SIZE, full_priority=False)
        self.assertEqual(host.bridge.decoded.emitted, [])
        self.assertIsNone(host.decode_cache.get((path, THUMB_SIZE)))
        self.assertEqual(scheduler.pending, {})

    def test_lookup_failure_emits_failed(self) -> None:
        path = Path("/photos/a.jpg")
        folder = _FolderCache(raises=True)
        scheduler, host = _make(folder)
        scheduler.submit_decode(path, THUMB_SIZE, full_priority=False)
        self.assertEqual(len(host.bridge.failed.emitted), 1)
        self.assertEqual(host.bridge.failed.emitted[0][0], str(path))
        self.assertEqual(scheduler.pending, {})

    def test_foreground_full_future_tracked_for_current_path(self) -> None:
        path = Path("/photos/a.jpg")
        scheduler, host = _make()
        host.current_path = path
        current = _PendingExecutor()
        scheduler.current_decode_executor = current
        scheduler.submit_decode(path, 2048, full_priority=True)
        key = (path, 2048)
        self.assertIn(key, scheduler.pending)
        self.assertIn(key, scheduler.foreground_full_futures)
        self.assertEqual(len(current.calls), 1)

    def test_background_used_for_non_current_full(self) -> None:
        path = Path("/photos/a.jpg")
        scheduler, host = _make()
        host.current_path = Path("/photos/other.jpg")
        current = _PendingExecutor()
        background = _PendingExecutor()
        scheduler.current_decode_executor = current
        scheduler.background_decode_executor = background
        scheduler.submit_decode(path, 2048, full_priority=True)
        self.assertEqual(current.calls, [])
        self.assertEqual(len(background.calls), 1)
        self.assertNotIn((path, 2048), scheduler.foreground_full_futures)

    def test_video_thumbnail_fallback_requests_thumbnailer(self) -> None:
        path = Path("/photos/clip.mp4")
        folder = _FolderCache(loads={})  # SQLite miss
        scheduler, host = _make(folder)
        scheduler.submit_video_thumbnail(path, visible_priority=False)
        self.assertEqual(host.video_thumbnailer.requested, [path])
        self.assertEqual(scheduler.pending, {})

    def test_video_thumbnail_inactive_workspace_no_request(self) -> None:
        path = Path("/photos/clip.mp4")
        folder = _FolderCache(loads={})
        scheduler, host = _make(folder)
        host.workspace_active = False
        scheduler.submit_video_thumbnail(path, visible_priority=False)
        self.assertEqual(host.video_thumbnailer.requested, [])

    def test_cancel_pending_clears_all_bookkeeping(self) -> None:
        scheduler, _ = _make()
        key = (Path("/photos/a.jpg"), THUMB_SIZE)
        scheduler.pending[key] = Future()
        scheduler.foreground_full_futures[(Path("/photos/b.jpg"), 2048)] = Future()
        scheduler.visible_thumb_pending.add(key)
        scheduler.cancel_pending()
        self.assertEqual(scheduler.pending, {})
        self.assertEqual(scheduler.foreground_full_futures, {})
        self.assertEqual(scheduler.visible_thumb_pending, set())

    def test_cancel_pending_tolerates_cancel_callbacks_mutating_pending(self) -> None:
        scheduler, _ = _make()
        first_key = (Path("/photos/a.jpg"), THUMB_SIZE)
        second_key = (Path("/photos/b.jpg"), THUMB_SIZE)
        first = Future()
        second = Future()
        scheduler.pending = {first_key: first, second_key: second}
        first.add_done_callback(lambda _done: scheduler.pending.pop(second_key, None))

        scheduler.cancel_pending()

        self.assertTrue(first.cancelled())
        self.assertTrue(second.cancelled())
        self.assertEqual(scheduler.pending, {})

    def test_abandon_retires_decode_pools(self) -> None:
        scheduler, _ = _make()
        current = _PendingExecutor()
        background = _PendingExecutor()
        scheduler.current_decode_executor = current
        scheduler.background_decode_executor = background
        scheduler.abandon_preview_decode_work()
        self.assertTrue(current.shut)
        self.assertTrue(background.shut)
        self.assertIsNone(scheduler.current_decode_executor)
        self.assertIsNone(scheduler.background_decode_executor)

    def test_shutdown_closes_lookup_pools(self) -> None:
        scheduler, _ = _make()
        scheduler.shutdown()
        self.assertTrue(scheduler.background_cache_lookup_executor.shut)
        self.assertTrue(scheduler.visible_thumb_cache_lookup_executor.shut)

    def test_lazy_executor_getters_create_once(self) -> None:
        scheduler, _ = _make()
        first = scheduler._current_decode_executor()
        second = scheduler._current_decode_executor()
        self.assertIs(first, second)
        first.shutdown(wait=False)

    def test_process_pools_by_default(self) -> None:
        from concurrent.futures import ProcessPoolExecutor

        scheduler, _ = _make()
        executor = scheduler._current_decode_executor()
        self.assertIsInstance(executor, ProcessPoolExecutor)
        executor.shutdown(wait=False)

    def test_thread_pools_when_processes_disabled(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        host = _Host(_FolderCache())
        scheduler = DecodeScheduler(
            host,
            thumb_size=THUMB_SIZE,
            original_size=ORIGINAL_SIZE,
            current_workers=1,
            background_workers=1,
            visible_thumb_workers=1,
            visible_thumb_lookup_workers=1,
            use_processes=False,
        )
        for getter in (
            scheduler._current_decode_executor,
            scheduler._background_decode_executor,
            scheduler._visible_thumb_decode_executor,
        ):
            executor = getter()
            self.assertIsInstance(executor, ThreadPoolExecutor)
        scheduler.shutdown()


if __name__ == "__main__":
    unittest.main()
