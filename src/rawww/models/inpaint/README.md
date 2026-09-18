# LaMa ONNX

`lama_fp32.onnx`: экспорт big-lama от Carve, вход 512×512, FP32, opset 17.

`lama_fp32_1024.onnx`: тот же checkpoint и граф, повторно экспортированный с
фиксированным входом 1024×1024. Его URL задаётся константой `HD_MODEL_URL` в
`rawww.inpaint_pipeline`; пустое значение нарочно не запускает скачивание до
публикации файла на сервере. SHA-256:
`49a400fa4e2e8198cc2011753820b9a5dd8bfdee4d26b19e583e37ef2690d1c4`.

- Файл при первом открытии утилиты скачивается с https://shotsync.ru/static/ctrlka/models/lama_fp32.onnx
- Исходная модель: https://github.com/advimman/lama
- Лицензия модели и экспорта: Apache-2.0.
- SHA-256: `1faef5301d78db7dda502fe59966957ec4b79dd64e16f03ed96913c7a4eb68d6`.
- Portable-сборка кладёт файл в `data/models/inpaint`; установленная — в
  пользовательский кэш Контрольки. Загрузка и проверка вручную:
  `uv run python scripts/download_inpaint_model.py`.

Suvorov et al., Resolution-robust Large Mask Inpainting with Fourier
Convolutions, WACV 2022. При распространении сохраняйте LICENSE модели.
