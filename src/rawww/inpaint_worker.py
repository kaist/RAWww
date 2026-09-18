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
import gc
import json
import os
from pathlib import Path
import queue
import sys
import threading

from .inpaint_pipeline import (
    DeepOad, EditableImage, ImageConflictError, LamaInpainter, crop_image, crop_with_inpaint, ensure_horizon_model,
    ensure_inpaint_model, fingerprint, load_image, make_mask, save_image, scale_strokes,
    straighten_image, to_srgb,
)


_DRAFT_SIDE = 1920
_MODEL_IDLE_BEFORE_LOAD = 1.5


def _lower_model_loader_thread_priority() -> None:
    """Отдаёт CPU и диск кадрам, не понижая приоритет всего воркера."""
    try:
        if sys.platform == "win32":
            import ctypes

            # THREAD_MODE_BACKGROUND_BEGIN понижает не только планирование CPU,
            # но и приоритет I/O: именно чтение ONNX-файлов не должно тормозить JPEG.
            ctypes.windll.kernel32.SetThreadPriority(ctypes.windll.kernel32.GetCurrentThread(), 0x00010000)
        elif sys.platform.startswith("linux"):
            # Linux хранит nice для каждого task (native thread), а не только
            # для процесса, поэтому decode остаётся на обычном приоритете.
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
        elif sys.platform == "darwin":
            import ctypes

            # macOS QoS определяет планирование и I/O; BACKGROUND равен 0x09.
            pthread = ctypes.CDLL("/usr/lib/libSystem.B.dylib").pthread_set_qos_class_self_np
            pthread.argtypes = (ctypes.c_uint, ctypes.c_int)
            pthread.restype = ctypes.c_int
            pthread(0x09, 0)
    except (AttributeError, OSError):
        pass


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
        self.originals: dict[str, EditableImage] = {}
        self.history: dict[str, list[Image.Image]] = {}
        self.redo_history: dict[str, list[Image.Image]] = {}
        self.rotation_bases = {}
        self.rotation_angles: dict[str, float] = {}
        self.inferences: set[str] = set()
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
        with self.lock:
            frame = self.frames.get(path)
            # Кадр до 1920 px уже показывается целиком, поэтому prepare_full()
            # намеренно не создаёт Future. Не обращаться к нему как к draft:
            # иначе Enter на обычном небольшом JPEG завершается KeyError.
            if frame is not None and not frame.draft:
                if path not in self.originals:
                    self.originals[path] = EditableImage(
                        frame.path, frame.image.copy(), frame.signature, frame.format, frame.exif,
                        frame.icc, frame.xmp, frame.dpi, frame.draft,
                    )
                return frame, frame.image.width / draft_size[0], frame.image.height / draft_size[1]
        self.prepare_full(path)
        with self.lock:
            request = self.full_requests[path]
        full = request.result()
        # Пока пользователь рисовал, фон мог успеть заменить draft полным
        # кадром в кэше. Маска всё равно имеет размеры показанного draft.
        with self.lock:
            # Оригинал нужен и после автосохранения: файл на диске уже может
            # содержать правку, но команда сброса должна вернуть первый кадр.
            if path not in self.originals:
                self.originals[path] = EditableImage(
                    full.path, full.image.copy(), full.signature, full.format, full.exif,
                    full.icc, full.xmp, full.dpi, full.draft,
                )
        return full, full.image.width / draft_size[0], full.image.height / draft_size[1]

    def reset(self, path: str) -> EditableImage:
        """Возвращает исходные пиксели, сохранённые до первой правки кадра."""
        with self.lock:
            frame = self.frames[path]
            original = self.originals.get(path)
        if original is None:
            original = load_image(Path(path))
            with self.lock:
                self.originals[path] = EditableImage(
                    original.path, original.image.copy(), original.signature, original.format,
                    original.exif, original.icc, original.xmp, original.dpi, original.draft,
                )
        with self.lock:
            revision = max(frame.revision, self.versions.get(path, 0)) + 1
            self._remember_history(path, frame.image)
            frame.image = original.image.copy()
            frame.format = original.format
            frame.exif = original.exif
            frame.icc = original.icc
            frame.xmp = original.xmp
            frame.dpi = original.dpi
            frame.draft = original.draft
            frame.revision = self.versions[path] = revision
            self.rotation_bases[path] = original.image.copy()
            self.rotation_angles[path] = 0.0
            return frame

    def replace_pixels(self, frame: EditableImage, image) -> None:
        """Публикует неизменяемый снимок под новым, не повторяющимся номером."""
        with self.lock:
            path = str(frame.path)
            self._remember_history(path, frame.image)
            revision = max(frame.revision, self.versions.get(path, 0)) + 1
            frame.image = image
            frame.revision = self.versions[path] = revision
            # Следующий ручной поворот начинается от результата другой геометрической правки.
            self.rotation_bases[path] = image.copy()
            self.rotation_angles[path] = 0.0

    def begin_inpaint(self, path: str, revision: int) -> None:
        """Закрепляет кадр на время AI-задачи, чтобы кэш не вытеснил её результат."""
        with self.lock:
            frame = self.frames[path]
            if revision != frame.revision:
                raise RuntimeError("stale_revision")
            self.inferences.add(path)

    def finish_inpaint(self, path: str) -> None:
        """Снимает защиту кэша после публикации результата или ошибки инференса."""
        with self.lock:
            self.inferences.discard(path)
            self._trim()

    def _remember_history(self, path: str, image: Image.Image) -> None:
        """Сохраняет точное предыдущее состояние и ограничивает историю десятью шагами."""
        history = self.history.setdefault(path, [])
        history.append(image.copy())
        del history[:-10]
        self.redo_history[path] = []

    def _restore_history(self, path: str, *, redo: bool) -> EditableImage:
        """Меняет текущее изображение с соседним состоянием истории без повторного инференса."""
        with self.lock:
            frame = self.frames[path]
            source = self.redo_history if redo else self.history
            destination = self.history if redo else self.redo_history
            if not source.get(path):
                raise RuntimeError("history_empty")
            destination.setdefault(path, []).append(frame.image.copy())
            del destination[path][:-10]
            frame.image = source[path].pop()
            revision = max(frame.revision, self.versions.get(path, 0)) + 1
            frame.revision = self.versions[path] = revision
            self.rotation_bases[path] = frame.image.copy()
            self.rotation_angles[path] = 0.0
            return frame

    def undo(self, path: str) -> EditableImage:
        """Возвращает предыдущее состояние текущего кадра."""
        return self._restore_history(path, redo=False)

    def redo(self, path: str) -> EditableImage:
        """Повторяет отменённое состояние текущего кадра."""
        return self._restore_history(path, redo=True)

    def history_state(self, path: str) -> tuple[bool, bool]:
        """Сообщает доступность отмены и повтора для интерфейса."""
        with self.lock:
            return bool(self.history.get(path)), bool(self.redo_history.get(path))

    def rotate_from_base(self, frame: EditableImage, degrees: float) -> None:
        """Строит результат от одной базы, чтобы поворот туда-обратно не мыл пиксели."""
        with self.lock:
            path = str(frame.path)
            base = self.rotation_bases.setdefault(path, frame.image.copy())
        result = straighten_image(EditableImage(
            frame.path, base, frame.signature, frame.format, frame.exif, frame.icc,
            frame.xmp, frame.dpi,
        ), -degrees)
        with self.lock:
            revision = max(frame.revision, self.versions.get(path, 0)) + 1
            self._remember_history(path, frame.image)
            frame.image = result
            frame.revision = self.versions[path] = revision
            self.rotation_angles[path] = degrees

    def _trim(self) -> None:
        """Ограничивает чистый кэш числом кадров и реальными байтами пикселей."""
        total = sum(f.image.width*f.image.height*len(f.image.getbands()) for f in self.frames.values())
        for key, frame in list(self.frames.items()):
            if total <= self.budget and len(self.frames) <= 5:
                break
            if key != self.current and key not in self.inferences and not frame.pending and frame.revision == frame.saved_revision:
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
            if frame and (frame.pending or path in self.inferences):
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
    """Открывает кадры сразу, пока модели готовятся в фоновых потоках."""
    output_lock = threading.Lock()
    commands = queue.Queue()
    latest = {"open": 0}
    models: dict[str, LamaInpainter | DeepOad | None] = {"inpaint": None, "horizon": None}
    models_lock = threading.Lock()
    inpaint_quality = "sd"
    loaded_inpaint_quality: str | None = None
    model_load_timer: threading.Timer | None = None
    models_loading_started = False

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
    # Две ONNX-сессии одновременно забивают диск и ядра при холодном старте.
    # Один загрузчик оставляет основному циклу возможность сразу декодировать
    # следующий кадр по команде навигации.
    loaders = ThreadPoolExecutor(max_workers=1, thread_name_prefix="inpaint-model")
    inpaint_jobs = ThreadPoolExecutor(max_workers=1, thread_name_prefix="inpaint-apply")
    # Несколько слотов не дают последнему кадру ждать старые decode, уже упёршиеся
    # в диск рядом с последовательной инициализацией модели. Устаревшие задания
    # выходят до чтения, поэтому одновременно реально работают лишь текущие кадры.
    open_jobs = ThreadPoolExecutor(max_workers=4, thread_name_prefix="inpaint-open")

    def emit_frame(kind: str, path: str, request: int, frame: EditableImage, *, angle=None) -> None:
        """Собирает ответ одинаково для главного цикла и завершившейся AI-задачи."""
        display = to_srgb(frame.image, frame.icc)
        can_undo, can_redo = store.history_state(path)
        emit(kind, payload=display.tobytes(), path=path, request=request,
             width=display.width, height=display.height, revision=frame.revision,
             saved_revision=frame.saved_revision, angle=angle,
             rotation_angle=store.rotation_angles.get(path, 0.0), can_undo=can_undo, can_redo=can_redo)

    def run_inpaint(task: dict) -> None:
        """Выполняет LaMa вне цикла команд, сохраняя возможность открыть другой кадр."""
        path = task["path"]
        try:
            frame, scale_x, scale_y = store.full_for_apply(path, tuple(task["draft_size"]))
            mask = make_mask(frame.image.size, scale_strokes(task["strokes"], scale_x, scale_y))
            with models_lock:
                model = models["inpaint"]
            if model is None:
                raise RuntimeError("inpaint_model_not_ready")
            result = model.apply(frame, mask)
            store.replace_pixels(frame, result)
            emit_frame("result", path, task["request"], frame)
            store.preload(task.get("neighbors", []))
        except Exception as exc:
            code = "external_change" if isinstance(exc, ImageConflictError) else str(exc)
            emit("error", path=path, request=task["request"], revision=task.get("revision", 0), error=code)
        finally:
            store.finish_inpaint(path)

    def open_frame(task: dict) -> None:
        """Читает кадр вне цикла команд: загрузка ONNX не должна съедать навигацию."""
        path = task["path"]
        request = task["request"]
        try:
            if request != latest["open"]:
                return
            store.current = path
            frame = store.get(path)
            if request != latest["open"]:
                return
            emit_frame("frame", path, request, frame)
            store.preload(task.get("neighbors", []))
            defer_model_loading()
        except Exception as exc:
            code = "external_change" if isinstance(exc, ImageConflictError) else str(exc)
            emit("error", path=path, request=request, revision=task.get("revision", 0), error=code)

    def load_inpaint(quality: str, *, lower_priority: bool = True) -> None:
        """Скачивает и создаёт LaMa вне очереди кадров и команд пользователя."""
        nonlocal loaded_inpaint_quality
        if lower_priority:
            _lower_model_loader_thread_priority()
        try:
            def progress(downloaded: int, total: int | None) -> None:
                emit("model_downloading", model="inpaint", quality=quality,
                     downloaded=downloaded, total=total or 0)

            path = ensure_inpaint_model(progress, quality)
            emit("model_loading", model="inpaint", quality=quality)
            loaded = LamaInpainter(path)
            with models_lock:
                models["inpaint"] = loaded
                loaded_inpaint_quality = quality
            emit("model_ready", model="inpaint", quality=quality)
        except Exception as exc:
            emit("model_error", model="inpaint", quality=quality, error=str(exc))

    def switch_inpaint(quality: str) -> None:
        """Освобождает прежнюю ONNX-сессию до загрузки выбранного качества."""
        nonlocal loaded_inpaint_quality
        emit("model_unloading", model="inpaint", quality=quality)
        with models_lock:
            previous = models["inpaint"]
            models["inpaint"] = None
            loaded_inpaint_quality = None
        del previous
        # ONNX Runtime освобождает нативные буферы вместе с последней ссылкой;
        # явный цикл не даёт двум тяжёлым сессиям встретиться в памяти.
        gc.collect()
        # Этот поток затем снова выполняет inpaint, поэтому его системный
        # приоритет не понижаем навсегда ради одноразовой загрузки.
        load_inpaint(quality, lower_priority=False)

    def load_horizon() -> None:
        """Скачивает Deep-OAD после LaMa в том же загрузчике, не конкурируя с ней за диск."""
        _lower_model_loader_thread_priority()
        try:
            def progress(downloaded: int, total: int | None) -> None:
                emit("model_downloading", model="horizon", downloaded=downloaded, total=total or 0)

            path = ensure_horizon_model(progress)
            emit("model_loading", model="horizon")
            loaded = DeepOad(path)
            with models_lock:
                models["horizon"] = loaded
            emit("model_ready", model="horizon")
        except Exception as exc:
            emit("model_error", model="horizon", error=str(exc))

    def start_model_loading() -> None:
        """Запускает модели после паузы, когда навигация уже не нуждается в декодере."""
        nonlocal models_loading_started
        if models_loading_started:
            return
        models_loading_started = True
        loaders.submit(load_inpaint, inpaint_quality)
        loaders.submit(load_horizon)

    def defer_model_loading() -> None:
        """Переносит тяжёлую инициализацию ONNX после каждого нового кадра."""
        nonlocal model_load_timer
        if models_loading_started:
            return
        if model_load_timer is not None:
            model_load_timer.cancel()
        model_load_timer = threading.Timer(_MODEL_IDLE_BEFORE_LOAD, start_model_loading)
        model_load_timer.daemon = True
        model_load_timer.start()

    try:
        emit("ready")
        while True:
            task = commands.get()
            command = task["command"]
            if command == "close":
                break
            path = task.get("path", "")
            request = task.get("request", 0)
            try:
                if command == "set_inpaint_quality":
                    quality = task.get("quality")
                    if quality not in {"sd", "hd"}:
                        raise ValueError("unknown_inpaint_quality")
                    inpaint_quality = quality
                    with models_lock:
                        already_loaded = loaded_inpaint_quality == quality and models["inpaint"] is not None
                    if already_loaded:
                        emit("model_ready", model="inpaint", quality=quality)
                    elif models_loading_started:
                        inpaint_jobs.submit(switch_inpaint, quality)
                    continue
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
                if command == "open":
                    open_jobs.submit(open_frame, task)
                    continue
                store.current = path
                frame = store.get(path)
                if command == "apply":
                    store.begin_inpaint(path, task["revision"])
                    inpaint_jobs.submit(run_inpaint, task)
                    continue
                elif command == "straighten":
                    frame, _, _ = store.full_for_apply(path, tuple(task["draft_size"]))
                    with models_lock:
                        horizon_model = models["horizon"]
                    if horizon_model is None:
                        raise RuntimeError("horizon_model_not_ready")
                    angle = horizon_model.predict_angle(frame)
                    # Небольшая граница сохраняет замысел съёмки и не превращает
                    # инструмент горизонта в автоматический поворот портретов.
                    if abs(angle) > 15:
                        raise RuntimeError(f"horizon_angle_out_of_range:{angle:.1f}")
                    store.replace_pixels(frame, straighten_image(frame, angle))
                elif command == "rotate":
                    frame, _, _ = store.full_for_apply(path, tuple(task["draft_size"]))
                    store.rotate_from_base(frame, float(task["degrees"]))
                elif command == "reset":
                    frame = store.reset(path)
                elif command == "undo":
                    frame = store.undo(path)
                elif command == "redo":
                    frame = store.redo(path)
                elif command == "crop":
                    frame, scale_x, scale_y = store.full_for_apply(path, tuple(task["draft_size"]))
                    left, top, right, bottom = task["box"]
                    box = (
                        left * scale_x, top * scale_y, right * scale_x, bottom * scale_y,
                    )
                    if task.get("inpaint_edges"):
                        with models_lock:
                            model = models["inpaint"]
                        if model is None:
                            raise RuntimeError("inpaint_model_not_ready")
                        result = crop_with_inpaint(frame, box, model)
                    else:
                        result = crop_image(frame, box)
                    store.replace_pixels(frame, result)
                emit_frame("result" if command in {"straighten", "rotate", "crop", "reset", "undo", "redo"} else "frame",
                           path, request, frame, angle=angle if command == "straighten" else None)
                store.preload(task.get("neighbors", []))
            except Exception as exc:
                code = "external_change" if isinstance(exc, ImageConflictError) else str(exc)
                emit("save_error" if command == "save" else "error", path=path,
                     request=request, revision=task.get("revision", 0), error=code)
    finally:
        # Сначала останавливаем открытия: каждое из них способно перезапустить
        # таймер моделей, а после остановки загрузчика это уже поздно.
        open_jobs.shutdown(wait=True, cancel_futures=True)
        if model_load_timer is not None:
            model_load_timer.cancel()
        loaders.shutdown(wait=False, cancel_futures=True)
        inpaint_jobs.shutdown(wait=True, cancel_futures=True)
        store.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
