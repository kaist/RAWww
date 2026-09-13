# LaMa ONNX

`lama_fp32.onnx`: экспорт big-lama от Carve, вход 512×512, FP32, opset 17.

- Файл при первом открытии утилиты скачивается с https://shotsync.ru/static/ctrlka/models/lama_fp32.onnx
- Исходная модель: https://github.com/advimman/lama
- Лицензия модели и экспорта: Apache-2.0.
- SHA-256: `1faef5301d78db7dda502fe59966957ec4b79dd64e16f03ed96913c7a4eb68d6`.
- Portable-сборка кладёт файл в `data/models/inpaint`; установленная — в
  пользовательский кэш Контрольки. Загрузка и проверка вручную:
  `uv run python scripts/download_inpaint_model.py`.

Suvorov et al., Resolution-robust Large Mask Inpainting with Fourier
Convolutions, WACV 2022. При распространении сохраняйте LICENSE модели.
