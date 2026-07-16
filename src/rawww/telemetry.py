## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Отправляет анонимные сессии Контрольки, не задерживая запуск и закрытие."""

from __future__ import annotations

import json
import platform as platform_module
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from urllib.request import Request, urlopen
from uuid import uuid4

from PySide6.QtCore import QSettings, QTimer

from .error_log import error_log_path
from .version import __version__ as APP_VERSION


TELEMETRY_URL = "https://shotsync.ru/api/ctrlka/telemetry/"
DISABLED_SETTING = "telemetry/disable_usage_statistics"
INSTALLATION_SETTING = "telemetry/installation_id"


class TelemetryClient:
    """Держит локальную очередь сессий и отправляет её редкими фоновыми пакетами."""

    def __init__(self, settings: QSettings, parent=None) -> None:
        self.settings = settings
        self.timer = QTimer(parent)
        self.timer.setInterval(5 * 60 * 1000)
        self.timer.timeout.connect(self.submit_snapshot)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="telemetry")
        self.lock = threading.Lock()
        self.started_at = datetime.now(UTC)
        self.started_monotonic = monotonic()
        self.session_id = str(uuid4())
        installation_id = settings.value(INSTALLATION_SETTING, "", str)
        if not installation_id:
            installation_id = str(uuid4())
            settings.setValue(INSTALLATION_SETTING, installation_id)
        self.installation_id = installation_id

    @property
    def enabled(self) -> bool:
        return not self.settings.value(DISABLED_SETTING, False, bool)

    def start(self) -> None:
        """Откладывает первую отправку, чтобы запуск приложения оставался мгновенным."""
        if not self.enabled:
            # Отказ от статистики распространяется и на неотправленную очередь
            # прошлых запусков, иначе включение опции позже было бы неожиданным.
            self._save_pending([])
            return
        QTimer.singleShot(60_000, self.submit_snapshot)
        self.timer.start()

    def stop(self) -> None:
        """Сохраняет финальную длительность локально, не делая сетью закрытие окна."""
        self.timer.stop()
        if self.enabled:
            self._append_pending(self._session_payload())
        self.executor.shutdown(wait=False, cancel_futures=False)

    def submit_snapshot(self) -> None:
        """Передаёт накопленные сессии в единственном фоновом потоке."""
        if not self.enabled:
            return
        self.executor.submit(self._send_pending, self._session_payload())

    def _session_payload(self) -> dict[str, object]:
        system = sys.platform
        name = {"win32": "Windows", "darwin": "macOS"}.get(system, "Linux" if system.startswith("linux") else system)
        return {
            "installation_id": self.installation_id,
            "session_id": self.session_id,
            "started_at": self.started_at.isoformat(),
            "duration_seconds": max(1, round(monotonic() - self.started_monotonic)),
            "platform": name,
            "os_version": f"{platform_module.release()} ({platform_module.version()})"[:128],
            "app_version": APP_VERSION,
        }

    @staticmethod
    def _queue_path() -> Path:
        return error_log_path().with_name("telemetry.json")

    def _load_pending(self) -> list[dict[str, object]]:
        try:
            payload = json.loads(self._queue_path().read_text(encoding="utf-8"))
            return payload if isinstance(payload, list) else []
        except (OSError, ValueError):
            return []

    def _save_pending(self, sessions: list[dict[str, object]]) -> None:
        path = self._queue_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(sessions[-20:]), encoding="utf-8")
            temporary.replace(path)
        except OSError:
            return

    def _append_pending(self, session: dict[str, object]) -> None:
        with self.lock:
            queued = [item for item in self._load_pending() if item.get("session_id") != session["session_id"]]
            queued.append(session)
            self._save_pending(queued)

    def _send_pending(self, snapshot: dict[str, object]) -> None:
        with self.lock:
            pending = [item for item in self._load_pending() if item.get("session_id") != snapshot["session_id"]]
        payload = {"sessions": [*pending, snapshot][-20:]}
        try:
            request = Request(
                TELEMETRY_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=5) as response:
                if response.status != 200 or not json.loads(response.read().decode("utf-8")).get("ok"):
                    raise OSError("Telemetry endpoint rejected the request")
        except (OSError, ValueError, json.JSONDecodeError):
            self._append_pending(snapshot)
            return
        with self.lock:
            self._save_pending([])
