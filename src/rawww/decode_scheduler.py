"""Asynchronous decode scheduling extracted from ``Workspace``.

The scheduler owns the process/thread pools and the in-flight bookkeeping
(``pending`` futures, the visible-thumbnail set and the foreground full-frame
futures). Decode results are still delivered through the host's ``DecodeBridge``
signals; the host (``Workspace``) remains the single source of truth for the
current directory/path, the folder cache and the workspace-active flag, which
the completion callbacks read live so that stale results are discarded exactly
as before.
"""

from __future__ import annotations

from concurrent.futures import Executor, Future, ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from typing import Protocol

from .decode_cache import DecodeCache
from .imaging import (
    DecodedImage,
    PixelImage,
    decode_original_pixels,
    decode_pixels,
    decode_thumbnail_pixels,
    pixel_to_decoded,
)


class DecodeHost(Protocol):
    """Live workspace state the completion callbacks read on the UI thread."""

    closing: bool
    current_dir: Path
    current_path: Path | None
    workspace_active: bool
    folder_cache: object
    decode_cache: DecodeCache
    bridge: object
    video_thumbnailer: object


class DecodeScheduler:
    def __init__(
        self,
        host: DecodeHost,
        *,
        thumb_size: int,
        original_size: int,
        current_workers: int,
        background_workers: int,
        visible_thumb_workers: int,
        visible_thumb_lookup_workers: int,
        use_processes: bool = True,
    ) -> None:
        self._host = host
        self._thumb_size = thumb_size
        self._original_size = original_size
        self._current_workers = current_workers
        self._background_workers = background_workers
        self._visible_thumb_workers = visible_thumb_workers
        self._decode_executor_cls: type[Executor] = (
            ProcessPoolExecutor if use_processes else ThreadPoolExecutor
        )

        self.current_decode_executor: Executor | None = None
        self.background_decode_executor: Executor | None = None
        self.visible_thumb_decode_executor: Executor | None = None
        self.background_cache_lookup_executor = ThreadPoolExecutor(max_workers=1)
        self.visible_thumb_cache_lookup_executor = ThreadPoolExecutor(max_workers=visible_thumb_lookup_workers)

        self.pending: dict[tuple[Path, int], Future] = {}
        self.foreground_full_futures: dict[tuple[Path, int], Future] = {}
        self.visible_thumb_pending: set[tuple[Path, int]] = set()

    def submit_decode(
        self,
        path: Path,
        max_size: int,
        *,
        full_priority: bool,
        visible_priority: bool = False,
    ) -> None:
        if self._host.closing:
            return
        key = (path, max_size)
        cached = self._host.decode_cache.get(key)
        if cached is not None:
            self._host.bridge.decoded.emit((cached, max_size))
            return
        if key in self.pending:
            return
        if self._host.folder_cache is None:
            return

        if max_size == self._thumb_size:
            cache = self._host.folder_cache
            executor = self.visible_thumb_cache_lookup_executor if visible_priority else self.background_cache_lookup_executor
            future = executor.submit(cache.load, path, max_size)
            self.pending[key] = future
            if visible_priority:
                self.visible_thumb_pending.add(key)
            future.add_done_callback(
                lambda done, p=path, s=max_size, fp=full_priority, vp=visible_priority: self._cache_lookup_done(
                    p, s, fp, vp, done
                )
            )
            return
        # Full-view images deliberately bypass the disk cache. They are decoded
        # from the source on demand and live only in the bounded RAM LRU.
        self._submit_process_decode(path, max_size, full_priority=full_priority, visible_priority=visible_priority)

    def submit_video_thumbnail(self, path: Path, *, visible_priority: bool) -> None:
        """Use RAM, then SQLite, before falling back to Qt frame decoding."""
        key = (path, self._thumb_size)
        preview = self._host.decode_cache.thumbnail_get(path)
        if preview is not None:
            self._host.bridge.decoded.emit(
                (DecodedImage(path=path, image=preview, width=preview.width(), height=preview.height()), self._thumb_size)
            )
            return
        cached = self._host.decode_cache.get(key)
        if cached is not None:
            self._host.bridge.decoded.emit((cached, self._thumb_size))
            return
        if key in self.pending or self._host.folder_cache is None:
            return
        executor = self.visible_thumb_cache_lookup_executor if visible_priority else self.background_cache_lookup_executor
        future = executor.submit(self._host.folder_cache.load, path, self._thumb_size)
        self.pending[key] = future
        if visible_priority:
            self.visible_thumb_pending.add(key)
        future.add_done_callback(
            lambda done, p=path, vp=visible_priority: self._video_thumbnail_cache_lookup_done(p, vp, done)
        )

    def _video_thumbnail_cache_lookup_done(self, path: Path, visible_priority: bool, future: Future) -> None:
        key = (path, self._thumb_size)
        if self.pending.get(key) is future:
            self.pending.pop(key, None)
        self.visible_thumb_pending.discard(key)
        if self._host.closing or future.cancelled() or path.parent != self._host.current_dir:
            return
        try:
            decoded = future.result()
        except Exception as exc:
            self._host.bridge.failed.emit(str(path), str(exc))
            return
        if decoded is not None:
            self._host.decode_cache.put(key, decoded)
            self._host.bridge.decoded.emit((decoded, self._thumb_size))
            return
        if self._host.workspace_active:
            self._host.video_thumbnailer.request(path)

    def _submit_process_decode(
        self,
        path: Path,
        max_size: int,
        *,
        full_priority: bool,
        visible_priority: bool = False,
    ) -> None:
        if self._host.closing:
            return
        key = (path, max_size)
        if key in self.pending:
            return
        is_foreground = False
        if visible_priority:
            executor = self._visible_thumb_decode_executor()
        elif full_priority:
            executor = self._current_decode_executor() if path == self._host.current_path else self._background_decode_executor()
            if path == self._host.current_path:
                is_foreground = True
        else:
            executor = self._background_decode_executor()
        try:
            decoder = decode_original_pixels if max_size == self._original_size else (decode_pixels if full_priority else decode_thumbnail_pixels)
            future = executor.submit(decoder, path) if max_size == self._original_size else executor.submit(decoder, path, max_size)
        except RuntimeError:
            # Shutdown may begin between the guard above and submit because
            # cache callbacks execute on worker threads.
            if self._host.closing:
                return
            raise
        self.pending[key] = future
        if is_foreground:
            self.foreground_full_futures[key] = future
        future.add_done_callback(lambda done, p=path, s=max_size: self._decode_done(p, s, done))

    def _cache_lookup_done(
        self,
        path: Path,
        max_size: int,
        full_priority: bool,
        visible_priority: bool,
        future: Future,
    ) -> None:
        key = (path, max_size)
        if self.pending.get(key) is future:
            self.pending.pop(key, None)
        self.visible_thumb_pending.discard(key)
        if self._host.closing or path.parent != self._host.current_dir:
            return
        if future.cancelled():
            return
        try:
            decoded = future.result()
        except Exception as exc:
            self._host.bridge.failed.emit(str(path), str(exc))
            return
        if decoded is not None:
            self._host.decode_cache.put((path, max_size), decoded)
            self._host.bridge.decoded.emit((decoded, max_size))
            return
        self._submit_process_decode(path, max_size, full_priority=full_priority, visible_priority=visible_priority)

    def _decode_done(self, path: Path, max_size: int, future: Future) -> None:
        key = (path, max_size)
        if self.pending.get(key) is future:
            self.pending.pop(key, None)
        self.visible_thumb_pending.discard(key)
        if self.foreground_full_futures.get(key) is future:
            self.foreground_full_futures.pop(key, None)
        if self._host.closing or path.parent != self._host.current_dir:
            return
        if future.cancelled():
            return
        try:
            result = future.result()
            if isinstance(result, PixelImage):
                if self._host.folder_cache is not None and max_size == self._thumb_size:
                    self._host.folder_cache.store_pixels(result, max_size)
                decoded = pixel_to_decoded(result)
            else:
                decoded = result
            self._host.decode_cache.put((path, max_size), decoded)
            self._host.bridge.decoded.emit((decoded, max_size))
        except Exception as exc:
            self._host.bridge.failed.emit(str(path), str(exc))

    def _current_decode_executor(self) -> Executor:
        if self.current_decode_executor is None:
            self.current_decode_executor = self._decode_executor_cls(max_workers=self._current_workers)
        return self.current_decode_executor

    def _background_decode_executor(self) -> Executor:
        if self.background_decode_executor is None:
            self.background_decode_executor = self._decode_executor_cls(max_workers=self._background_workers)
        return self.background_decode_executor

    def _visible_thumb_decode_executor(self) -> Executor:
        if self.visible_thumb_decode_executor is None:
            self.visible_thumb_decode_executor = self._decode_executor_cls(max_workers=self._visible_thumb_workers)
        return self.visible_thumb_decode_executor

    def abandon_preview_decode_work(self) -> None:
        """Let a newly opened folder start decoding without an old queue ahead.

        ``Future.cancel`` cannot stop a RAW decode that is already executing.
        Retiring the executors still cancels all queued work, while fresh
        executors let the new folder's visible cards begin immediately.
        Running old workers are allowed to finish and their results are
        discarded by the folder checks in the completion callbacks.
        """
        for attribute in (
            "current_decode_executor",
            "background_decode_executor",
            "visible_thumb_decode_executor",
        ):
            executor = getattr(self, attribute)
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
                setattr(self, attribute, None)

    def cancel_pending(self) -> None:
        """Cancel queued work and drop all in-flight bookkeeping."""
        pending, self.pending = self.pending, {}
        self.foreground_full_futures.clear()
        self.visible_thumb_pending.clear()
        for future in pending.values():
            future.cancel()

    def shutdown(self) -> None:
        if self.current_decode_executor is not None:
            self.current_decode_executor.shutdown(wait=False, cancel_futures=True)
        if self.background_decode_executor is not None:
            self.background_decode_executor.shutdown(wait=False, cancel_futures=True)
        if self.visible_thumb_decode_executor is not None:
            self.visible_thumb_decode_executor.shutdown(wait=False, cancel_futures=True)
        self.background_cache_lookup_executor.shutdown(wait=False, cancel_futures=True)
        self.visible_thumb_cache_lookup_executor.shutdown(wait=False, cancel_futures=True)
