## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Процесс предпросмотра и пакетной ретуши.

Пакет запускается одноразовым процессом и завершается сам. Предпросмотр же
живёт вместе с окном: процесс читает задачи построчно и держит распакованный
кадр с масками кожи, поэтому движение ползунка пересчитывает только цветовые
этапы, а не декодирование, сегментацию и разбор лица заново.
"""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import ctypes
from dataclasses import replace
import gc
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from .imaging import RAW_EXTENSIONS, _rawpy, decode_original_pixels, decode_pixels
from .retouch_pipeline import MASK_SIDE, RetouchSettings, SkinMasks, SkinRetoucher, crop_masks
from .runtime_paths import data_path


_output_lock = threading.Lock()


def _event(kind: str, **values) -> None:
    """Отправляет протокол всегда в UTF-8, независимо от кодовой страницы Windows.

    Кадры пакета считаются параллельно, поэтому строка пишется под замком:
    перемешанные половинки JSON интерфейс разобрать не сможет.
    """
    line = json.dumps({"event": kind, **values}, ensure_ascii=False).encode("utf-8") + b"\n"
    with _output_lock:
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()


def _read(path: Path, max_side: int | None, region: tuple[int, int, int, int] | None = None) -> tuple[np.ndarray, tuple[int, int], tuple[int, int]]:
    if region is not None and path.suffix.lower() not in RAW_EXTENSIONS:
        # JPEG обычно распаковывается лениво: вырезаем только область, которая
        # нужна экрану, плюс заранее переданный контекст для маски и размытия.
        with Image.open(path) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
            full_size = image.size
            x, y, width, height = region
            left, top = max(0, x), max(0, y)
            right, bottom = min(full_size[0], x + width), min(full_size[1], y + height)
            return np.asarray(image.crop((left, top, right, bottom)), dtype=np.uint8), (left, top), full_size
    # Для вписанного preview JPEG декодируется сразу в нужном черновом размере:
    # ``decode_pixels`` использует Pillow.draft, не распаковывая оригинал целиком.
    pixel = decode_pixels(path, max_side) if max_side else decode_original_pixels(path)
    image = Image.frombytes("RGBA", (pixel.width, pixel.height), pixel.pixels).convert("RGB")
    if path.suffix.lower() not in RAW_EXTENSIONS:
        with Image.open(path) as opened:
            native = ImageOps.exif_transpose(opened).size
    else:
        native = image.size
    return np.asarray(image, dtype=np.uint8), (0, 0), native


def _write(path: Path, rgb: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    image = Image.fromarray(rgb, "RGB")
    image.save(temporary, format="JPEG", quality=95, subsampling=0)
    os.replace(temporary, path)


def _jpeg_bytes(rgb: np.ndarray, quality: int = 95) -> bytes:
    """Кодирует preview в памяти, чтобы не создавать временный файл на диске."""
    from io import BytesIO

    buffer = BytesIO()
    Image.fromarray(rgb, "RGB").save(buffer, format="JPEG", quality=quality, subsampling=0)
    return buffer.getvalue()


class _Frame:
    """Распакованный кадр предпросмотра вместе с масками кожи.

    Кэш живёт до смены снимка или области 100 %: маски зависят только от
    пикселей, а ползунки на них не влияют.
    """

    def __init__(self, key: tuple, rgb: np.ndarray, origin: tuple[int, int], full_size: tuple[int, int]) -> None:
        self.key = key
        self.rgb = rgb
        self.origin = origin
        self.full_size = full_size
        self.masks: SkinMasks | None = None
        self.sent_before = False


class _Preview:
    """Обрабатывает поток задач предпросмотра одним процессом с кэшем кадра."""

    def __init__(self, retoucher: SkinRetoucher) -> None:
        self._retoucher = retoucher
        self._frame: _Frame | None = None
        self._whole: tuple[str, SkinMasks, tuple[int, int]] | None = None

    def run(self, task: dict, settings: RetouchSettings, *, stale) -> None:
        source = Path(task["source"])
        region = tuple(task["region"]) if task.get("region") else None
        key = (str(source), task.get("max_side"), region)
        frame = self._frame
        if frame is None or frame.key != key:
            rgb, origin, full_size = _read(source, task.get("max_side"), region)
            frame = _Frame(key, rgb, origin, full_size)
            self._frame = frame
        if frame.masks is None:
            frame.masks = self._masks(source, frame, region)
        if stale():
            return
        base = self._retoucher.retouch_skin(frame.rgb, replace(settings, neural_retouch=False), frame.masks)
        # Цветовой результат уходит на экран сразу: нейроретушь на кадре превью
        # стоит в разы больше, а ждать её, чтобы увидеть тон, незачем.
        self._send(frame, self._retoucher.finish(base, settings), exact=not settings.neural_retouch)
        if not settings.neural_retouch or stale():
            return
        # Нейроретушь ложится на кадр до цвета и таблицы, иначе точный
        # предпросмотр отличался бы от результата пакета порядком этапов.
        exact = self._retoucher.neural_retouch(base, frame.masks.skin, settings.neural_strength)
        self._send(frame, self._retoucher.finish(exact, settings), exact=True)

    def _masks(self, source: Path, frame: _Frame, region: tuple[int, int, int, int] | None) -> SkinMasks:
        """Даёт маски кадра, а для кропа 100 % — вырезку из масок целого снимка.

        По кропу маски считать нельзя: лицо крупным планом не влезает в него
        целиком, детектор молчит, парсинг лица не запускается и губы с глазами
        остаются в маске. Маски целого снимка кэшируются на снимок, поэтому
        прокрутка при 100 % не платит за сегментацию повторно.
        """
        if region is None:
            return self._retoucher.skin_masks(frame.rgb)
        cached = self._whole
        if cached is None or cached[0] != str(source):
            whole, _origin, _full = _read(source, MASK_SIDE)
            cached = (str(source), self._retoucher.skin_masks(whole), (whole.shape[1], whole.shape[0]))
            self._whole = cached
        _key, masks, size = cached
        share = (size[0] / frame.full_size[0], size[1] / frame.full_size[1])
        left, top = frame.origin
        box = (
            left * share[0],
            top * share[1],
            (left + frame.rgb.shape[1]) * share[0],
            (top + frame.rgb.shape[0]) * share[1],
        )
        return crop_masks(masks, box, (frame.rgb.shape[1], frame.rgb.shape[0]))

    def _send(self, frame: _Frame, result: np.ndarray, *, exact: bool) -> None:
        values = {
            "jpeg": base64.b64encode(_jpeg_bytes(result, 92)).decode("ascii"),
            "origin": frame.origin,
            "full_size": frame.full_size,
            "exact": exact,
        }
        if not frame.sent_before:
            # Оригинал для сравнения передаётся один раз на кадр: он не зависит
            # от настроек, а лишний JPEG на каждое движение ползунка — это
            # кодирование и пересылка нескольких мегабайт впустую.
            values["before"] = base64.b64encode(_jpeg_bytes(frame.rgb, 92)).decode("ascii")
            frame.sent_before = True
        _event("preview", **values)


class _BatchControl:
    """Пауза и остановка пакета, пришедшие в поток ввода по ходу работы.

    Команду читает поток-читатель, а видит её обработка кадров на своей
    границе: убивать процесс ради остановки нельзя, иначе в папке останется
    недописанный ``.tmp`` вместо кадра.
    """

    def __init__(self) -> None:
        self._running = threading.Event()
        self._running.set()
        self._stopped = False

    def apply(self, command: str) -> None:
        if command == "pause":
            self._running.clear()
        elif command == "resume":
            self._running.set()
        elif command == "stop":
            self._stopped = True
            self._running.set()

    def wait_while_paused(self) -> bool:
        self._running.wait()
        return not self._stopped


def _memory_gb() -> float:
    """Оценивает объём оперативной памяти машины, не заводя зависимостей.

    Нужна не точность, а порядок: по этому числу решается, сколько кадров
    держать в работе. Если платформа ответа не дала, считаем машину скромной.
    """
    try:
        if sys.platform == "win32":
            class _Status(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _Status()
            status.dwLength = ctypes.sizeof(_Status)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            return status.ullTotalPhys / (1024 ** 3)
        if sys.platform == "darwin":
            output = subprocess.run(
                ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5
            )
            return int(output.stdout.strip()) / (1024 ** 3)
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024 ** 3)
    except Exception:
        return 4.0


def _batch_megapixels(tasks: list[dict]) -> float:
    """Крупнейший кадр пакета в мегапикселях по заголовкам файлов.

    Размер читается без декодирования: Pillow берёт его из заголовка, а для RAW
    его сообщает libraw по оглавлению файла. Кадры в пакете бывают разного
    размера, а правило памяти должно исходить из худшего, а не из среднего.
    """
    largest = 0.0
    for task in tasks:
        path = Path(task["source"])
        try:
            if path.suffix.lower() in RAW_EXTENSIONS:
                with _rawpy().imread(str(path)) as raw:
                    width, height = raw.sizes.width, raw.sizes.height
            else:
                with Image.open(path) as opened:
                    width, height = opened.size
        except Exception:
            # Битый или недоступный файл: об этом сообщит сама обработка кадра.
            continue
        max_side = task.get("max_side")
        if max_side:
            share = min(1.0, float(max_side) / max(width, height, 1))
            width, height = width * share, height * share
        largest = max(largest, width * height / 1e6)
    return largest or 24.0


def _frame_workers(
    cpus: int | None = None,
    neural: bool = True,
    memory_gb: float | None = None,
    megapixels: float = 24.0,
) -> int:
    """Сколько кадров пакета считается одновременно.

    Нейроретушь занимает все ядра плитками, а декодирование, матирование,
    выравнивание тона, D&B, LUT и запись JPEG живут в одном потоке и на
    многоядерной машине занимают одно ядро из десятка. Соседние кадры занимают
    эти простои: пока один считает цвет, другой кормит общий пул плиток. Без
    нейроретуши ни один этап по ядрам не растёт, и единственный способ занять
    процессор целиком — считать кадров столько, сколько ядер.

    Потолок ставит память, а не ядра, и мерится она мегапикселями конкретного
    пакета, а не абстрактным «кадром»: замер даёт около 300 МБ на кадр в
    работе при шести мегапикселях — около 60 МБ на мегапиксель. Два гигабайта
    остаются моделям, системе и интерфейсу. Больше шести кадров не берётся
    никогда: дальше выигрыш съедают переключения и подкачка.
    """
    if cpus is None:
        cpus = getattr(os, "process_cpu_count", os.cpu_count)() or 4
    if memory_gb is None:
        memory_gb = _memory_gb()
    per_frame_gb = max(4.0, megapixels) * .06
    by_memory = int(max(memory_gb - 2.0, per_frame_gb) / per_frame_gb)
    if not neural:
        return max(1, min(6, cpus, by_memory))
    if cpus < 4:
        # Двухядерной машине параллельные кадры дают только расход памяти:
        # плитки нейроретуши и так выбирают всё, что есть.
        return 1
    # С нейроретушью параллельные кадры закрывают только простои
    # однопоточных этапов, поэтому их число растёт с ядрами медленно.
    by_cpu = 2 if cpus < 9 else min(6, cpus // 3)
    return max(1, min(6, by_cpu, by_memory))


def _batch(
    retoucher: SkinRetoucher,
    tasks: list[dict],
    settings: RetouchSettings,
    control: _BatchControl | None = None,
) -> None:
    """Считает пакет конвейером из нескольких кадров в работе.

    Потоки, а не процессы: сессии ONNX потокобезопасны, а декодирование,
    numpy-этапы и кодирование JPEG отпускают GIL. Отдельные процессы пришлось
    бы грузить тремя моделями каждый, и они же поделили бы ядра между собой.

    Остановка и пауза срабатывают на границе подачи нового кадра: кадры в
    работе дописываются, иначе в папке остался бы недописанный ``.tmp``.

    Кончившаяся память не должна уносить весь пакет: кадр откладывается, окно
    сужается до одного кадра, и отложенное досчитывается в конце, когда
    параллельные кадры уже освободили свои копии.
    """
    total = len(tasks)
    counted = 0
    failed = 0
    counter = threading.Lock()
    postponed: list[dict] = []
    tight = threading.Event()

    def render(task: dict) -> None:
        nonlocal counted
        source = Path(task["source"])
        original, _origin, _full_size = _read(source, task.get("max_side"))
        target = Path(task["target"])
        _write(target, retoucher.process(original, settings))
        with counter:
            counted += 1
            done = counted
        # Порядок готовых кадров теперь любой, поэтому интерфейсу идёт
        # общее число сделанного, а не номер задачи в списке.
        _event("progress", done=done, total=total, source=str(source), target=str(target))

    def attempt(task: dict) -> None:
        """Считает кадр, а нехватку памяти превращает в отложенную задачу."""
        nonlocal failed
        try:
            render(task)
        except MemoryError:
            # Освобождать надо сразу: следующий кадр уже стоит в очереди пула, и
            # без сборки он упрётся в те же несколько float32-копий.
            gc.collect()
            tight.set()
            with counter:
                postponed.append(task)
        except Exception as exc:
            # Один битый или занятый снимок не должен уносить весь пакет: остальные
            # кадры считаются дальше, а провалившийся называется по имени.
            with counter:
                failed += 1
            _event("error", message=f"{Path(task['source']).name}: {exc}")

    workers = _frame_workers(
        neural=bool(settings.neural_retouch and settings.neural_strength > 0),
        megapixels=_batch_megapixels(tasks),
    )
    stopped = False
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="retouch-frame") as executor:
        pending: set[Future] = set()
        for task in tasks:
            if control is not None and not control.wait_while_paused():
                stopped = True
                break
            while len(pending) >= (1 if tight.is_set() else workers):
                # Окно поданных задач ограниченное: иначе пауза и остановка
                # увидели бы уже всю очередь в работе.
                finished, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in finished:
                    future.result()
            pending.add(executor.submit(attempt, task))
        for future in pending:
            future.result()
    for task in postponed:
        # Второй заход строго по одному кадру и уже без соседей в памяти. Если
        # не хватило и теперь, кадр честно объявляется пропущенным: остальной
        # пакет от одного тяжёлого снимка страдать не должен.
        if control is not None and not control.wait_while_paused():
            stopped = True
            break
        try:
            render(task)
        except MemoryError:
            gc.collect()
            failed += 1
            _event("error", message=f"Не хватило памяти на кадр {Path(task['source']).name}")
        except Exception as exc:
            failed += 1
            _event("error", message=f"{Path(task['source']).name}: {exc}")
    if stopped:
        _event("stopped", done=counted, total=total)
    # Итог пакета считается по событиям, а не по коду выхода процесса: на
    # выгрузке моделей нативные библиотеки иногда возвращают мусорный код, а
    # все кадры при этом уже на диске.
    _event("completed", done=counted, total=total, failed=failed)


def _jobs(control: _BatchControl) -> queue.Queue:
    """Читает задачи из stdin отдельным потоком: select на трубе не работает в Windows.

    Команды управления разбираются здесь же и в очередь не попадают:
    обработка пакета до своего конца не вернётся к чтению очереди, а пауза
    нужна именно во время работы.
    """
    incoming: queue.Queue = queue.Queue()

    def reader() -> None:
        # QProcess передаёт JSON в UTF-8, а ``sys.stdin`` на Windows может быть
        # обёрнут в cp1251/cp866 и испортить путь с кириллицей.
        for line in sys.stdin.buffer:
            if not line.strip():
                continue
            try:
                message = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            command = message.get("command") if isinstance(message, dict) else None
            if command:
                control.apply(str(command))
                continue
            incoming.put(message)
        incoming.put(None)

    threading.Thread(target=reader, daemon=True).start()
    return incoming


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("job", nargs="?", type=Path)
    parser.add_argument("--stdin", action="store_true")
    args = parser.parse_args(argv)
    try:
        retoucher: SkinRetoucher | None = None
        preview: _Preview | None = None
        if args.stdin:
            control = _BatchControl()
            incoming = _jobs(control)
            closed = False
            while True:
                job = incoming.get()
                if job is None:
                    return 0
                while not incoming.empty():
                    # Пока считался старый предпросмотр, ползунок уехал дальше:
                    # промежуточные задачи никому не нужны. Конец потока при
                    # этом только запоминается: задачу в руках надо досчитать,
                    # иначе пакет из одной строки с сразу закрытым каналом
                    # молча пропал бы.
                    newer = incoming.get()
                    if newer is None:
                        closed = True
                        break
                    # За брошенную задачу всё равно отчитываемся: интерфейс
                    # считает ответы, и без этого спиннер висел бы до конца.
                    _event("finished")
                    job = newer
                if retoucher is None:
                    retoucher = SkinRetoucher(data_path("models") / "retouch")
                settings = RetouchSettings(**job["settings"])
                if job.get("preview"):
                    if preview is None:
                        preview = _Preview(retoucher)
                    try:
                        preview.run(job["tasks"][0], settings, stale=lambda: not incoming.empty())
                    except Exception as exc:
                        # Битая таблица или недоступный файл не должны уносить
                        # живой процесс превью: модели грузились секунды.
                        _event("error", message=str(exc))
                    _event("finished")
                    if closed:
                        return 0
                    continue
                _batch(retoucher, job["tasks"], settings, control)
                _event("finished")
                return 0
        if args.job is None:
            raise RuntimeError("Не передана задача ретуши")
        job = json.loads(args.job.read_text(encoding="utf-8"))
        retoucher = SkinRetoucher(data_path("models") / "retouch")
        _batch(retoucher, job["tasks"], RetouchSettings(**job["settings"]))
        _event("finished")
        return 0
    except Exception as exc:
        _event("error", message=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
