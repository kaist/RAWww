## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Создаёт совместимую с DirectML копию ONNX-графа LaMa в пользовательском кэше."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile

from .runtime_paths import application_cache_path


_REWRITE_VERSION = 1
_EXPECTED_MATMULS = 144


def directml_lama_model(source: Path) -> Path:
    """Сворачивает пакетные измерения FFT-MatMul, сохраняя исходник неизменным.

    Экспорт LaMa содержит 144 операции ``[64,64] @ [1,192,33,64,1]``.
    DirectML отвергает их пятиразмерную форму. Сворачивание первых трёх
    измерений второго входа сохраняет математику и даёт DirectML ранг три.
    """
    import onnx
    from onnx import TensorProto, helper

    with source.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    directory = application_cache_path() / "models" / "inpaint" / "directml"
    target = directory / f"{source.stem}-{digest[:16]}-v{_REWRITE_VERSION}.onnx"
    if target.is_file():
        return target
    model = onnx.load(str(source))
    nodes = []
    changed = 0
    for node in model.graph.node:
        if node.op_type != "MatMul" or "/rttn/MatMul_" not in node.name or node.name.rsplit("_", 1)[-1] not in {"2", "3", "4", "5"}:
            nodes.append(node)
            continue
        changed += 1
        prefix = f"rawww_dml_{changed}"
        shape = prefix + "_shape"
        last_two = prefix + "_last_two"
        flat_shape = prefix + "_flat_shape"
        flat_input = prefix + "_flat_input"
        flat_output = prefix + "_flat_output"
        nodes.extend((
            helper.make_node("Shape", [node.input[1]], [shape], name=prefix + "_shape_node"),
            helper.make_node("Slice", [shape, "rawww_dml_slice_start", "rawww_dml_slice_end"], [last_two], name=prefix + "_slice_node"),
            helper.make_node("Concat", ["rawww_dml_minus_one", last_two], [flat_shape], axis=0, name=prefix + "_concat_node"),
            helper.make_node("Reshape", [node.input[1], flat_shape], [flat_input], name=prefix + "_flatten_node"),
            helper.make_node("MatMul", [node.input[0], flat_input], [flat_output], name=prefix + "_matmul_node"),
            helper.make_node("Reshape", [flat_output, shape], list(node.output), name=prefix + "_restore_node"),
        ))
    if changed != _EXPECTED_MATMULS:
        raise RuntimeError(f"unsupported_lama_graph:{changed}")
    model.graph.ClearField("node")
    model.graph.node.extend(nodes)
    model.graph.initializer.extend((
        helper.make_tensor("rawww_dml_slice_start", TensorProto.INT64, [1], [3]),
        helper.make_tensor("rawww_dml_slice_end", TensorProto.INT64, [1], [5]),
        helper.make_tensor("rawww_dml_minus_one", TensorProto.INT64, [1], [-1]),
    ))
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".lama-dml-", suffix=".onnx", dir=directory)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        onnx.save(model, str(temporary))
        if not target.is_file():
            os.replace(temporary, target)
        return target
    finally:
        temporary.unlink(missing_ok=True)
