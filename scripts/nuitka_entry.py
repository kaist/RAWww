## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Точка входа для сборки Nuitka.

Nuitka, в отличие от PyInstaller, не выставляет ``sys.frozen``. Приложение же
опирается на этот флаг, чтобы отличить собранную поставку от запуска из
исходников: по нему выбираются пути к ресурсам, отключается обращение к Git за
версией и включается ``multiprocessing.freeze_support`` при spawn дочерних
процессов на Windows. Флаг выставляется до импорта ``rawww``, потому что
``runtime_paths`` вычисляет расположение ресурсов уже на этапе импорта.
"""

import sys

sys.frozen = True

from rawww import main


if __name__ == "__main__":
    main()
