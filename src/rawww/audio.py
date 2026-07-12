"""Temporary per-folder local WAV transcription pipeline."""

from __future__ import annotations

import json
from concurrent.futures import Future, ProcessPoolExecutor
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QUrl, Signal, Slot, Qt
from PySide6.QtMultimedia import QAudioDecoder, QAudioFormat

from .cache import FolderCache
from .worker_priority import lower_background_priority

SAMPLE_RATE = 16_000
MODEL_PATH = Path(__file__).with_name("models") / "vosk-model-small-ru-0.22"
_model_instance = None


def _model():
    global _model_instance
    if _model_instance is None:
        from vosk import Model, SetLogLevel
        SetLogLevel(-1)
        _model_instance = Model(str(MODEL_PATH))
    return _model_instance


def recognize_pcm(pcm: bytes) -> str:
    from vosk import KaldiRecognizer
    lower_background_priority()
    recognizer = KaldiRecognizer(_model(), SAMPLE_RATE)
    parts = []
    for offset in range(0, len(pcm), 8_000):
        if recognizer.AcceptWaveform(pcm[offset:offset + 8_000]):
            text = str(json.loads(recognizer.Result()).get("text") or "").strip()
            if text:
                parts.append(text)
    text = str(json.loads(recognizer.FinalResult()).get("text") or "").strip()
    return " ".join([*parts, *([text] if text else [])])


class _Decoder(QObject):
    decoded = Signal(object, bytes)
    failed = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.decoder: QAudioDecoder | None = None
        self.path: Path | None = None
        self.chunks: list[bytes] = []
        self.failed_once = False

    @Slot(object)
    def decode(self, value: object) -> None:
        self.path, self.chunks, self.failed_once = Path(value), [], False
        if self.decoder is None:
            self.decoder = QAudioDecoder(self)
            self.decoder.bufferReady.connect(self._buffer)
            self.decoder.finished.connect(self._finished)
            self.decoder.error.connect(self._error)
        fmt = QAudioFormat()
        fmt.setSampleRate(SAMPLE_RATE)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        self.decoder.stop()
        self.decoder.setAudioFormat(fmt)
        self.decoder.setSource(QUrl.fromLocalFile(str(self.path)))
        self.decoder.start()

    @Slot()
    def stop(self) -> None:
        if self.decoder is not None:
            self.decoder.stop()

    def _buffer(self) -> None:
        buffer = self.decoder.read() if self.decoder is not None else None
        if buffer is not None and buffer.isValid():
            self.chunks.append(bytes(buffer.constData()))

    def _finished(self) -> None:
        if self.path is not None and not self.failed_once:
            path, pcm = self.path, b"".join(self.chunks)
            self.path = None
            # Qt's Windows backend otherwise keeps the WAV handle open.
            if self.decoder is not None:
                self.decoder.setSource(QUrl())
            self.decoded.emit(path, pcm)

    def _error(self, _value) -> None:
        if self.path is not None and not self.failed_once:
            self.failed_once = True
            path = self.path
            self.path = None
            if self.decoder is not None:
                self.decoder.setSource(QUrl())
            self.failed.emit(path)


class AudioTranscriptionPipeline(QObject):
    """One Qt decoder and one Vosk process, both released after the queue."""

    decode_requested = Signal(object)
    stop_requested = Signal()
    advance_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.advance_requested.connect(self._next)
        self.thread = None
        self.decoder = None
        self.executor = None
        self.queue = []
        self.active = None
        self.cache = None
        self.callback = None

    def scan(self, paths: list[Path], cache: FolderCache, on_complete=None) -> None:
        self.shutdown()
        media = {path.stem.casefold(): path for path in paths if path.is_file()}
        wavs = {path.stem.casefold(): path for path in cache.folder.glob("*.wav") if path.is_file()}
        self.queue = [(path, wavs[stem]) for stem, path in media.items() if stem in wavs and not cache.audio_transcript_is_current(path, wavs[stem])]
        if not self.queue:
            return
        self.cache, self.callback = cache, on_complete
        self.thread, self.decoder = QThread(self), _Decoder()
        self.decoder.moveToThread(self.thread)
        self.decode_requested.connect(self.decoder.decode, Qt.ConnectionType.QueuedConnection)
        self.stop_requested.connect(self.decoder.stop, Qt.ConnectionType.QueuedConnection)
        self.decoder.decoded.connect(self._decoded)
        self.decoder.failed.connect(lambda _path: self.advance_requested.emit())
        self.thread.start()
        self.advance_requested.emit()

    def _next(self) -> None:
        if not self.queue:
            self._finish()
            return
        self.active = self.queue.pop(0)
        self.decode_requested.emit(self.active[1])

    def _decoded(self, _wav: Path, pcm: bytes) -> None:
        if not pcm:
            self.advance_requested.emit()
            return
        if self.executor is None:
            self.executor = ProcessPoolExecutor(max_workers=1)
        future = self.executor.submit(recognize_pcm, pcm)
        future.add_done_callback(self._recognised)

    def _recognised(self, future: Future) -> None:
        active = self.active
        if active is None or self.cache is None:
            return
        try:
            transcript = future.result()
        except Exception:
            transcript = ""
        media, wav = active
        self.cache.store_audio_transcripts([(media.name, wav.name, transcript)])
        if self.callback is not None:
            self.callback([(str(media), wav.name, transcript)])
        self.active = None
        self.advance_requested.emit()

    def _finish(self) -> None:
        if self.thread is not None:
            self.thread.quit()
            self.thread.wait(1_000)
        if self.executor is not None:
            self.executor.shutdown(wait=False, cancel_futures=True)
        self.thread = self.decoder = self.executor = self.cache = self.callback = self.active = None

    def shutdown(self) -> None:
        self.queue.clear()
        if self.decoder is not None:
            self.stop_requested.emit()
        self._finish()
