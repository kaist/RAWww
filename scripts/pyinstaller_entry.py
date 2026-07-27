## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

import os
from pathlib import Path

from rawww import main


def _metadata_smoke(path: Path) -> None:
    """Проверяет чтение метаданных в собранном приложении без запуска Qt.

    Этот путь включается только переменной окружения сборки. Он ловит отсутствие
    нативной библиотеки pyexiv2, которое обычный старт пустого окна не замечает.
    """
    from rawww.exif import read_metadata

    metadata = read_metadata(str(path))
    required = (
        "EXIF:Orientation",
        "XMP:Rating",
        "EXIF:ExposureTime",
        "EXIF:Model",
        "Composite:SubSecDateTimeOriginal",
    )
    missing = [name for name in required if metadata.get(name) in (None, "")]
    if missing:
        raise RuntimeError(f"Metadata is incomplete ({', '.join(missing)}): {path}")
    print(f"Metadata smoke test passed: {path.name}")


if __name__ == "__main__":
    sample = os.environ.get("RAWWW_METADATA_SMOKE_PATH")
    if sample:
        _metadata_smoke(Path(sample))
    else:
        main()
