## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Асинхронный планировщик декодирования изображений для рабочей вкладки."""

from __future__ import annotations

from concurrent.futures import Executor, Future, ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from typing import Protocol

from .decode_cache import DecodeCache
from .error_log import log_exception
from .imaging import (
    DecodedImage,
    PixelImage,
    decode_original_pixels,
    decode_pixels,
    decode_thumbnail_pixels,
    pixel_to_decoded,
)
from .task_lifecycle import retire_executor


class DecodeHost(Protocol):
    """Описывает данные рабочей вкладки, нужные обработчикам завершения."""

    closing: bool
    directory_generation: int
    current_dir: Path
    current_path: Path | None
    workspace_active: bool
    folder_cache: object
    decode_cache: DecodeCache
    bridge: object
    video_thumbnailer: object

    def queue_preview_cache_write(
        self, cache: object, pixel: PixelImage, max_size: int
    ) -> None: ...


class DecodeScheduler:
    """Распределяет декодирование между срочной и фоновой очередями.

    Открытый кадр и видимые карточки получают исполнителей первыми, полный обход
    папки догоняет их позже. Перед публикацией результат сверяется с состоянием
    вкладки: пользователь всегда листает быстрее, чем хотелось бы старой задаче.
    """

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
        """Ставит декодирование в нужную очередь с подавлением одинаковых заданий."""
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
            generation = self._host.directory_generation
            future.add_done_callback(
                lambda done, p=path, s=max_size, fp=full_priority, vp=visible_priority, g=generation: self._queue_completion(
                    "cache", p, s, fp, vp, g, done
                )
            )
            return
        self._submit_process_decode(path, max_size, full_priority=full_priority, visible_priority=visible_priority)

    def submit_thumbnail_batch(self, paths: list[Path]) -> None:
        """Запрашивает фоновую пачку миниатюр одним чтением SQLite без приоритета экрана."""
        if self._host.closing or self._host.folder_cache is None:
            return
        requested = []
        for path in paths:
            key = (path, self._thumb_size)
            if self._host.decode_cache.get(key) is not None or key in self.pending:
                continue
            requested.append(path)
        if not requested:
            return
        cache = self._host.folder_cache
        future = self.background_cache_lookup_executor.submit(
            cache.load_batch, requested, self._thumb_size
        )
        for path in requested:
            self.pending[(path, self._thumb_size)] = future
        generation = self._host.directory_generation
        future.add_done_callback(
            lambda done, paths=tuple(requested), g=generation: self._queue_completion(
                "cache_batch", paths, g, done
            )
        )

    def submit_video_thumbnail(self, path: Path, *, visible_priority: bool) -> None:
        """Ищет кадр видео в RAM и SQLite, затем обращается к Qt-декодеру."""
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
        generation = self._host.directory_generation
        future.add_done_callback(
            lambda done, p=path, vp=visible_priority, g=generation: self._queue_completion(
                "video", p, vp, g, done
            )
        )

    def _video_thumbnail_cache_lookup_done(
        self, path: Path, visible_priority: bool, generation: int, future: Future
    ) -> None:
        key = (path, self._thumb_size)
        if self.pending.get(key) is future:
            self.pending.pop(key, None)
        self.visible_thumb_pending.discard(key)
        if (
            self._host.closing
            or generation != self._host.directory_generation
            or future.cancelled()
            or path.parent != self._host.current_dir
        ):
            return
        try:
            decoded = future.result()
        except FileNotFoundError:
            # Файл мог исчезнуть между чтением папки и ответом SQLite. Это
            # отменённая работа, а не ошибка декодера для пользователя.
            return
        except Exception as exc:
            log_exception(f"Не удалось получить превью видео: {path}", exc)
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
        """Отправляет CPU-декодирование в процесс и регистрирует callback результата."""
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
            decoder = (
                decode_original_pixels
                if max_size == self._original_size
                else decode_pixels
                if full_priority or visible_priority
                else decode_thumbnail_pixels
            )
            future = executor.submit(decoder, path) if max_size == self._original_size else executor.submit(decoder, path, max_size)
        except RuntimeError:
            if self._host.closing:
                return
            raise
        self.pending[key] = future
        if is_foreground:
            self.foreground_full_futures[key] = future
        generation = self._host.directory_generation
        future.add_done_callback(
            lambda done, p=path, s=max_size, g=generation: self._queue_completion(
                "decode", p, s, g, done
            )
        )

    def _queue_completion(self, kind: str, *payload: object) -> None:
        """Передаёт изменение состояния планировщика в главный поток Qt."""
        signal = getattr(self._host.bridge, "schedulerFinished", None)
        if signal is None:
            self.handle_completion((kind, *payload))
            return
        signal.emit((kind, *payload))

    def handle_completion(self, payload: tuple) -> None:
        """Обрабатывает готовую задачу уже в потоке владельца ``Workspace``."""
        kind, *arguments = payload
        if kind == "cache":
            self._cache_lookup_done(*arguments)
        elif kind == "cache_batch":
            self._cache_batch_lookup_done(*arguments)
        elif kind == "video":
            self._video_thumbnail_cache_lookup_done(*arguments)
        elif kind == "decode":
            self._decode_done(*arguments)

    def _cache_lookup_done(
        self,
        path: Path,
        max_size: int,
        full_priority: bool,
        visible_priority: bool,
        generation: int,
        future: Future,
    ) -> None:
        """Завершает поиск в дисковом кэше или запускает настоящее декодирование."""
        key = (path, max_size)
        if self.pending.get(key) is future:
            self.pending.pop(key, None)
        if (
            self._host.closing
            or generation != self._host.directory_generation
            or path.parent != self._host.current_dir
        ):
            self.visible_thumb_pending.discard(key)
            return
        if future.cancelled():
            self.visible_thumb_pending.discard(key)
            return
        try:
            decoded = future.result()
        except FileNotFoundError:
            # Удаление, перенос и пакетное переименование закономерно обгоняют
            # фоновую очередь превью. Поздний промах просто отбрасывается.
            return
        except Exception as exc:
            self.visible_thumb_pending.discard(key)
            log_exception(f"Не удалось прочитать кэш превью: {path}", exc)
            self._host.bridge.failed.emit(str(path), str(exc))
            return
        if decoded is not None:
            self.visible_thumb_pending.discard(key)
            self._host.decode_cache.put((path, max_size), decoded)
            self._host.bridge.decoded.emit((decoded, max_size))
            return
        # Приоритет относится ко всему запросу, а не только к чтению SQLite.
        # Иначе после промаха кэша таймер решит, что срочная очередь свободна,
        # и успеет набить её карточками из прежней области экрана.
        self._submit_process_decode(path, max_size, full_priority=full_priority, visible_priority=visible_priority)
        if key not in self.pending:
            self.visible_thumb_pending.discard(key)

    def _cache_batch_lookup_done(
        self, paths: tuple[Path, ...], generation: int, future: Future
    ) -> None:
        """Раздаёт результат пакетного чтения и декодирует только отсутствующие миниатюры."""
        for path in paths:
            key = (path, self._thumb_size)
            if self.pending.get(key) is future:
                self.pending.pop(key, None)
        if (
            self._host.closing
            or generation != self._host.directory_generation
            or future.cancelled()
        ):
            return
        try:
            decoded = future.result()
        except Exception as exc:
            for path in paths:
                self._host.bridge.failed.emit(str(path), str(exc))
            return
        for path in paths:
            image = decoded.get(path)
            if image is not None:
                self._host.decode_cache.put((path, self._thumb_size), image)
                self._host.bridge.decoded.emit((image, self._thumb_size))
            elif path.is_file():
                self._submit_process_decode(
                    path, self._thumb_size, full_priority=False, visible_priority=False
                )

    def _decode_done(self, path: Path, max_size: int, generation: int, future: Future) -> None:
        key = (path, max_size)
        if self.pending.get(key) is future:
            self.pending.pop(key, None)
        self.visible_thumb_pending.discard(key)
        if self.foreground_full_futures.get(key) is future:
            self.foreground_full_futures.pop(key, None)
        if (
            self._host.closing
            or generation != self._host.directory_generation
            or path.parent != self._host.current_dir
        ):
            return
        if future.cancelled():
            return
        try:
            result = future.result()
            if isinstance(result, PixelImage):
                decoded = pixel_to_decoded(result)
            else:
                decoded = result
            self._host.decode_cache.put((path, max_size), decoded)
            self._host.bridge.decoded.emit((decoded, max_size))
            if (
                isinstance(result, PixelImage)
                and self._host.folder_cache is not None
                and max_size == self._thumb_size
            ):
                self._host.queue_preview_cache_write(
                    self._host.folder_cache, result, max_size
                )
        except FileNotFoundError:
            # Задача уже не относится к существующему пользовательскому файлу.
            return
        except Exception as exc:
            log_exception(f"Не удалось декодировать файл: {path}", exc)
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
        """Освобождает новую папку от очереди декодирования предыдущей.

        ``Future.cancel`` не умеет остановить уже начатое декодирование RAW.
        Поэтому старые пулы отправляются на завершение, а для новой папки сразу
        создаются свежие. Запоздавшие результаты отбрасываются проверкой папки в
        обработчике завершения — прошлому каталогу не дадут украсить новый.
        """
        for attribute in (
            "current_decode_executor",
            "background_decode_executor",
            "visible_thumb_decode_executor",
        ):
            executor = getattr(self, attribute)
            if executor is not None:
                retire_executor(executor)
                setattr(self, attribute, None)

    def cancel_pending(self) -> None:
        """Отменяет задания в очередях и очищает учёт незавершённой работы."""
        pending, self.pending = self.pending, {}
        self.foreground_full_futures.clear()
        self.visible_thumb_pending.clear()
        for future in pending.values():
            future.cancel()

    def shutdown(self) -> None:
        """Останавливает очереди и передаёт их общей финальной фазе приложения."""
        self.cancel_pending()
        if self.current_decode_executor is not None:
            retire_executor(self.current_decode_executor)
            self.current_decode_executor = None
        if self.background_decode_executor is not None:
            retire_executor(self.background_decode_executor)
            self.background_decode_executor = None
        if self.visible_thumb_decode_executor is not None:
            retire_executor(self.visible_thumb_decode_executor)
            self.visible_thumb_decode_executor = None
        retire_executor(self.background_cache_lookup_executor)
        retire_executor(self.visible_thumb_cache_lookup_executor)
