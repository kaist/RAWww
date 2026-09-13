## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Процесс удаления объектов: модель, ограниченный кэш и очередь записи.

Команды UI — JSON-строки. Ответ — JSON-заголовок и указанное в нём число
байтов RGB. Запись и предзагрузка идут отдельно от инференса; опубликованные
пиксели неизменяемы, а записи одного файла строго последовательны.
"""

from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import queue
import sys
import threading

from .inpaint_pipeline import (
    EditableImage, ImageConflictError, LamaInpainter, ensure_inpaint_model, fingerprint,
    load_image, make_mask, save_image, scale_strokes, to_srgb,
)


_DRAFT_SIDE = 1920


class ImageStore:
    """Владеет кадрами процесса; lock защищает публикацию, но не держится на диске.

    Поколение предзагрузки не позволяет позднему reader вернуть старые пиксели
    после сохранения/отмены. Несохранённые и записываемые кадры не вытесняются.
    """

    def __init__(self, budget: int = 384 * 1024 * 1024) -> None:
        self.frames: OrderedDict[str, EditableImage] = OrderedDict()
        # Номера правок переживают вытеснение пикселей: иначе поздний «saved 1»
        # мог бы ошибочно пометить следующую, снова названную «1», сохранённой.
        self.versions: dict[str, int] = {}
        self.lock = threading.RLock()
        self.budget = budget
        self.current = ""
        self.generation = 0
        self.closed = False
        self.preloader = ThreadPoolExecutor(max_workers=1, thread_name_prefix="inpaint-read")
        self.full_loader = ThreadPoolExecutor(max_workers=1, thread_name_prefix="inpaint-full")
        self.full_requests = {}
        self.writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="inpaint-save")

    def get(self, path: str) -> EditableImage:
        """Возвращает свою незавершённую правку либо проверенную дисковую версию."""
        signature = fingerprint(Path(path))
        with self.lock:
            old = self.frames.get(path)
            if old and (old.pending or old.revision != old.saved_revision or old.signature == signature):
                self.frames.move_to_end(path)
                return old
            generation = self.generation
        frame = load_image(Path(path), _DRAFT_SIDE)
        with self.lock:
            old = self.frames.get(path)
            if old and (old.pending or old.revision != old.saved_revision or old.signature == frame.signature):
                return old
            frame.revision = frame.saved_revision = self.versions.get(path, 0)
            if generation == self.generation or path == self.current:
                self.frames[path] = frame
                self._trim()
        return frame

    def _load_full(self, path: str) -> EditableImage:
        """Раскрывает исходник вне цикла команд, чтобы листание не ждало диск."""
        frame = load_image(Path(path))
        with self.lock:
            previous = self.frames.get(path)
            if previous and (previous.pending or previous.revision != previous.saved_revision):
                return previous
            frame.revision = frame.saved_revision = self.versions.get(path, 0)
            self.frames[path] = frame
            self._trim()
        return frame

    def prepare_full(self, path: str) -> None:
        """Начинает чтение полного кадра после первого штриха без ожидания UI."""
        with self.lock:
            frame = self.frames.get(path)
            if frame is not None and not frame.draft:
                return
            request = self.full_requests.get(path)
            if request is None or request.done():
                self.full_requests[path] = self.full_loader.submit(self._load_full, path)

    def full_for_apply(self, path: str, draft_size: tuple[int, int]) -> tuple[EditableImage, float, float]:
        """Возвращает оригинал и масштаб для маски, нарисованной на draft."""
        self.prepare_full(path)
        with self.lock:
            request = self.full_requests[path]
        full = request.result()
        # Пока пользователь рисовал, фон мог успеть заменить draft полным
        # кадром в кэше. Маска всё равно имеет размеры показанного draft.
        return full, full.image.width / draft_size[0], full.image.height / draft_size[1]

    def replace_pixels(self, frame: EditableImage, image) -> None:
        """Публикует неизменяемый снимок под новым, не повторяющимся номером."""
        with self.lock:
            path = str(frame.path)
            revision = max(frame.revision, self.versions.get(path, 0)) + 1
            frame.image = image
            frame.revision = self.versions[path] = revision

    def _trim(self) -> None:
        """Ограничивает чистый кэш числом кадров и реальными байтами пикселей."""
        total = sum(f.image.width*f.image.height*len(f.image.getbands()) for f in self.frames.values())
        for key, frame in list(self.frames.items()):
            if total <= self.budget and len(self.frames) <= 5:
                break
            if key != self.current and not frame.pending and frame.revision == frame.saved_revision:
                total -= frame.image.width*frame.image.height*len(frame.image.getbands())
                del self.frames[key]

    def preload(self, paths: list[str]) -> None:
        """Одна задача читает ближайших соседей и прекращается при смене кадра."""
        with self.lock:
            self.generation += 1
            generation = self.generation

        def run() -> None:
            for path in paths[:4]:
                with self.lock:
                    if self.closed or generation != self.generation:
                        return
                try:
                    self.get(path)
                except Exception:
                    # Ошибка соседа показывается только при его явном открытии.
                    pass
        self.preloader.submit(run)

    def discard(self, path: str) -> None:
        """Забывает правку только когда она не принадлежит активной записи."""
        with self.lock:
            frame = self.frames.get(path)
            if frame and frame.pending:
                raise RuntimeError("save_pending")
            self.generation += 1
            self.frames.pop(path, None)

    def save(self, path: str, revision: int, notify) -> None:
        """Снимок пикселей сохраняется в общей FIFO-очереди вне инференса."""
        with self.lock:
            frame = self.frames[path]
            if revision != frame.revision:
                raise RuntimeError("stale_revision")
            snapshot = frame.image
            frame.pending += 1

        def run() -> None:
            error = None
            try:
                with self.lock:
                    expected = frame.signature
                signature = save_image(frame, snapshot, expected)
                with self.lock:
                    frame.signature = signature
                    frame.saved_revision = revision
                    self.generation += 1
            except Exception as exc:
                error = "external_change" if isinstance(exc, ImageConflictError) else str(exc)
            finally:
                with self.lock:
                    frame.pending -= 1
                    self._trim()
                notify("save_error" if error else "saved", path=path, revision=revision, error=error)
        self.writer.submit(run)

    def shutdown(self) -> None:
        """Закрывает приём задач и дожидается пользовательских записей перед выходом."""
        with self.lock:
            self.closed = True
            self.generation += 1
        self.preloader.shutdown(wait=True, cancel_futures=True)
        self.full_loader.shutdown(wait=True, cancel_futures=True)
        self.writer.shutdown(wait=True)
        self.frames.clear()


def main() -> int:
    """Сессия LaMa живёт только до выхода дочернего процесса."""
    output_lock = threading.Lock()
    commands = queue.Queue()
    latest = {"open": 0}

    def emit(kind: str, payload: bytes = b"", **values) -> None:
        header = json.dumps({"event": kind, "bytes": len(payload), **values}, ensure_ascii=False).encode("utf-8")
        with output_lock:
            sys.stdout.buffer.write(header + b"\n")
            if payload:
                sys.stdout.buffer.write(payload)
            sys.stdout.buffer.flush()

    def read_commands() -> None:
        try:
            for line in sys.stdin.buffer:
                task = json.loads(line)
                if task["command"] == "open":
                    latest["open"] = task["request"]
                commands.put(task)
        finally:
            commands.put({"command": "close"})

    threading.Thread(target=read_commands, daemon=True).start()
    store = ImageStore()
    try:
        try:
            def download_progress(downloaded: int, total: int | None) -> None:
                emit("downloading", downloaded=downloaded, total=total or 0)

            model_path = ensure_inpaint_model(download_progress)
            emit("loading_model")
            model = LamaInpainter(model_path)
        except Exception as exc:
            emit("model_error", error=str(exc))
            return 1
        emit("ready")
        while True:
            task = commands.get()
            command = task["command"]
            if command == "close":
                break
            path = task.get("path", "")
            request = task.get("request", 0)
            try:
                if command == "save":
                    store.save(path, task["revision"], emit)
                    continue
                if command == "discard":
                    store.discard(path)
                    continue
                if command == "prepare":
                    store.prepare_full(path)
                    continue
                if command == "open" and request != latest["open"]:
                    continue
                store.current = path
                frame = store.get(path)
                if command == "apply":
                    if task["revision"] != frame.revision:
                        raise RuntimeError("stale_revision")
                    frame, scale_x, scale_y = store.full_for_apply(path, tuple(task["draft_size"]))
                    mask = make_mask(frame.image.size, scale_strokes(task["strokes"], scale_x, scale_y))
                    result = model.apply(frame, mask)
                    store.replace_pixels(frame, result)
                display = to_srgb(frame.image, frame.icc)
                emit("result" if command == "apply" else "frame", payload=display.tobytes(),
                     path=path, request=request, width=display.width, height=display.height,
                     revision=frame.revision, saved_revision=frame.saved_revision)
                store.preload(task.get("neighbors", []))
            except Exception as exc:
                code = "external_change" if isinstance(exc, ImageConflictError) else str(exc)
                emit("save_error" if command == "save" else "error", path=path,
                     request=request, revision=task.get("revision", 0), error=code)
    finally:
        store.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
