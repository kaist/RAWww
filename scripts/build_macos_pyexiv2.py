## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Собирает статический pyexiv2 для текущей macOS-архитектуры CI."""

from __future__ import annotations

import platform
import shutil
import site
import subprocess
import sys
from pathlib import Path

import pybind11


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "build" / "macos-pyexiv2"
EXIV2_VERSION = "v0.28.8"
PYEXIV2_VERSION = "v2.15.5"


def _run(*command: str, cwd: Path | None = None) -> None:
    """Выполняет шаг нативной сборки, сохраняя команду в журнале CI."""
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def _clone(url: str, revision: str, destination: Path) -> None:
    """Клонирует ровно закреплённый исходник, не смешивая его со старым кэшем."""
    if destination.exists():
        shutil.rmtree(destination)
    _run("git", "clone", "--depth", "1", "--branch", revision, url, str(destination))


def _site_packages() -> Path:
    """Возвращает каталог активного окружения, куда заменяется wheel pyexiv2."""
    candidates = [Path(path) for path in site.getsitepackages()]
    if not candidates:
        raise RuntimeError("Не найден site-packages активного Python-окружения")
    return candidates[0]


def main() -> None:
    """Собирает модуль, где Exiv2 статически вшит в расширение Python."""
    if sys.platform != "darwin":
        raise RuntimeError("Статическая сборка pyexiv2 предназначена только для macOS")

    architecture = platform.machine()
    deployment_target = "14.0"
    exiv2_source = WORK / "exiv2"
    pyexiv2_source = WORK / "pyexiv2"
    build_dir = WORK / "cmake"
    package_dir = _site_packages() / "pyexiv2"
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)

    _clone("https://github.com/Exiv2/exiv2.git", EXIV2_VERSION, exiv2_source)
    _clone("https://github.com/LeoHsiao1/pyexiv2.git", PYEXIV2_VERSION, pyexiv2_source)
    if package_dir.exists():
        shutil.rmtree(package_dir)
    shutil.copytree(pyexiv2_source / "pyexiv2", package_dir, dirs_exist_ok=True)
    (package_dir / "lib" / "__init__.py").write_text(
        "from . import exiv2api\n", encoding="utf-8"
    )

    cmake_lists = WORK / "CMakeLists.txt"
    cmake_lists.write_text(
        """cmake_minimum_required(VERSION 3.21)
project(rawww_pyexiv2 LANGUAGES CXX)
set(BUILD_SHARED_LIBS OFF CACHE BOOL \"\" FORCE)
set(EXIV2_BUILD_SAMPLES OFF CACHE BOOL \"\" FORCE)
set(EXIV2_BUILD_UNIT_TESTS OFF CACHE BOOL \"\" FORCE)
set(EXIV2_ENABLE_NLS OFF CACHE BOOL \"\" FORCE)
set(EXIV2_ENABLE_PNG OFF CACHE BOOL \"\" FORCE)
set(EXIV2_ENABLE_BROTLI OFF CACHE BOOL \"\" FORCE)
set(EXIV2_ENABLE_INIH OFF CACHE BOOL \"\" FORCE)
set(EXIV2_ENABLE_BMFF ON CACHE BOOL \"\" FORCE)
add_subdirectory(exiv2 EXCLUDE_FROM_ALL)
find_package(pybind11 CONFIG REQUIRED)
pybind11_add_module(exiv2api pyexiv2/pyexiv2/lib/exiv2api.cpp)
target_link_libraries(exiv2api PRIVATE exiv2lib)
# Exiv2 создаёт exiv2lib_export.h и exv_conf.h в корне build-каталога.
# При встраивании Exiv2 через add_subdirectory этот путь не попадает в
# публичный interface target, а binding включает заголовки напрямую.
target_include_directories(exiv2api PRIVATE "${CMAKE_BINARY_DIR}")
set_target_properties(exiv2api PROPERTIES LIBRARY_OUTPUT_DIRECTORY \"${OUTPUT_DIR}\")
""",
        encoding="utf-8",
    )
    _run(
        "cmake", "-S", str(WORK), "-B", str(build_dir),
        "-DCMAKE_BUILD_TYPE=Release",
        f"-DCMAKE_OSX_ARCHITECTURES={architecture}",
        f"-DCMAKE_OSX_DEPLOYMENT_TARGET={deployment_target}",
        f"-Dpybind11_DIR={pybind11.get_cmake_dir()}",
        f"-DOUTPUT_DIR={package_dir / 'lib'}",
    )
    _run("cmake", "--build", str(build_dir), "--config", "Release", "--parallel")
    print(f"Built static pyexiv2 for macOS {architecture}: {package_dir}")


if __name__ == "__main__":
    main()
