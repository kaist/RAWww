## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Скачивает проверенный ONNX-экспорт LaMa в поставляемые модели."""

from rawww.inpaint_pipeline import ensure_inpaint_model


def main() -> None:
    """Публикует модель только после полной загрузки и сверки SHA-256."""
    model = ensure_inpaint_model(
        lambda downloaded, total: print(
            f"\r{downloaded / 1024 / 1024:.0f} / {total / 1024 / 1024:.0f} MiB" if total else
            f"\r{downloaded / 1024 / 1024:.0f} MiB", end="", flush=True,
        )
    )
    print(f"\nDownloaded and verified: {model}")


if __name__ == "__main__":
    main()
