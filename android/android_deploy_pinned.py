"""Wrapper around ``pyside6-android-deploy`` that pins python-for-android's
``python3``/``hostpython3`` recipes to the CPython version matching the PySide6
Android wheels (cp311 -> 3.11).

Why this is needed
------------------
The PySide6 deploy tool hard-codes ``p4a.branch=develop``. That branch's
``python3``/``hostpython3`` recipes now build CPython 3.14, while the
PySide6/shiboken6 android wheels are ABI-tagged ``cp311`` and their ``.so``
files carry a hard ``DT_NEEDED`` on ``libpython3.11.so``. The APK therefore
builds "successfully" but crashes on launch with::

    java.lang.UnsatisfiedLinkError: dlopen failed:
        library "libpython3.11.so" not found: needed by .../libshiboken6.abi3.so

The fix is to build CPython 3.11 on device. We hook the deploy tool's
recipe-generation step and drop local ``python3``/``hostpython3`` recipes
(pinned to 3.11) into the same directory it passes to p4a as
``--local-recipes``. Each local recipe subclasses the upstream one and only
overrides ``version``; ``get_recipe_dir`` is pointed back at the upstream recipe
folder so the bundled patches (which support 3.11) still resolve.

Usage: drop-in replacement for ``pyside6-android-deploy`` -- same CLI args.
"""
from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

import PySide6.scripts as _scripts

SCRIPTS_DIR = str(Path(_scripts.__file__).resolve().parent)
sys.path.insert(0, SCRIPTS_DIR)

# CPython version matching the cp311 PySide6/shiboken6 android wheels.
PIN_VERSION = os.environ.get("RAWWW_ANDROID_PYTHON", "3.11.9")

_RECIPE_TMPL = """import os
from pythonforandroid.recipes.{module} import {cls} as _Base


class {cls}(_Base):
    version = {version!r}

    def get_recipe_dir(self):
        # Resolve bundled patches from the upstream recipe folder.
        return os.path.join(self.ctx.root_dir, "recipes", "{module}")


recipe = {cls}()
"""

_RECIPES = {
    "python3": _RECIPE_TMPL.format(module="python3", cls="Python3Recipe",
                                   version=PIN_VERSION),
    "hostpython3": _RECIPE_TMPL.format(module="hostpython3", cls="HostPython3Recipe",
                                       version=PIN_VERSION),
}


def _install_pin() -> None:
    from deploy_lib.android.android_config import AndroidConfig

    original = AndroidConfig.find_recipe_dir

    def patched(self):
        recipe_dir = original(self)
        if recipe_dir is None:
            recipe_dir = (self.generated_files_path / "recipes").resolve()
        recipe_dir = Path(recipe_dir)
        for name, content in _RECIPES.items():
            target = recipe_dir / name
            target.mkdir(parents=True, exist_ok=True)
            (target / "__init__.py").write_text(content, encoding="utf-8")
        return recipe_dir

    AndroidConfig.find_recipe_dir = patched


def main() -> None:
    _install_pin()
    script = os.path.join(SCRIPTS_DIR, "android_deploy.py")
    sys.argv = [script] + sys.argv[1:]
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()
