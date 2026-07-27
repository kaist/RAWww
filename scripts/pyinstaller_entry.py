## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

import json
import os
from pathlib import Path

from rawww import main


def _metadata_smoke(path: Path) -> None:
    """Проверяет чтение метаданных в собранном приложении без запуска Qt.

    Этот путь включается только переменной окружения сборки. Он ловит отсутствие
    нативной библиотеки pyexiv2, которое обычный старт пустого окна не замечает.
    """
    from rawww.exif import extract_metadata_batch

    results = extract_metadata_batch([str(path)])
    if len(results) != 1:
        raise RuntimeError(f"Metadata was not read: {path}")
    metadata = json.loads(results[0][1])
    required = ("orientation", "rating", "capture_settings", "camera", "original_datetime")
    missing = [name for name in required if not metadata.get(name) and metadata.get(name) != 0]
    if missing:
        raise RuntimeError(f"Metadata is incomplete ({', '.join(missing)}): {path}")
    print(f"Metadata smoke test passed: {path.name}")


if __name__ == "__main__":
    sample = os.environ.get("RAWWW_METADATA_SMOKE_PATH")
    if sample:
        _metadata_smoke(Path(sample))
    else:
        main()
