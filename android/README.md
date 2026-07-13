# Android build (ShotSync selection client)

The Android build is a trimmed profile of the app: only the ShotSync
"take a shooting for selection" workflow (login → shootings → download previews
→ thumbnail grid → viewer with rating/color/comment → mark sync). AI, XMP,
utilities, RAW/video decoding and the filesystem browser are compiled out via
`RAWWW_PROFILE=mobile` (see `src/rawww/platform_profile.py`).

Entry point: [`android/main.py`](main.py) → `rawww.mobile.main`.

## Prerequisites

- **JDK 17** (`java -version` → 17.x).
- **Android NDK r26b** and an **Android SDK** (platform + build-tools). The
  build machine must have plenty of disk/network — the toolchain and the
  Android Qt wheels are several GB in total.
- **Android PySide6 + shiboken6 wheels** (`aarch64`, `cp311`). Qt ships these
  separately from PyPI; download them from
  <https://download.qt.io/official_releases/QtForPython/pyside6/>
  matching the desktop version (e.g. `6.11.1`). The Android runtime uses
  Python 3.11 regardless of the host interpreter.

## Build

```bash
pip install -r .venv/lib/python3.*/site-packages/PySide6/scripts/requirements-android.txt

pyside6-android-deploy \
    --name RAWww \
    --wheel-pyside  /path/to/pyside6-6.11.1-6.11.1-cp311-cp311-android_aarch64.whl \
    --wheel-shiboken /path/to/shiboken6-6.11.1-6.11.1-cp311-cp311-android_aarch64.whl \
    --ndk-path /path/to/android-ndk-r26b \
    --sdk-path /path/to/android-sdk \
    -c android/pysidedeploy.spec
```

The generated `.apk` is written next to the spec. Extra Qt modules the app
needs (Network, WebSockets, Sql) are listed in `pysidedeploy.spec` under
`modules` in case auto-detection misses them.

## Status in the development sandbox

A real APK was **not** produced here. The toolchain was driven as far as the
sandbox allows, which established the exact requirements:

1. **Host Python must be ≤ 3.11.** `pyside6-android-deploy` refuses to run on
   the project's Python 3.12 (`RuntimeError: Android deployment requires Python
   version 3.11 or lower`, a buildozer limitation). A separate 3.11 build
   environment is required.
2. **Android Qt wheels download fine** — `pyside6`/`shiboken6`
   `cp311 … android_aarch64` wheels from
   <https://download.qt.io/official_releases/QtForPython/>.
3. **The NDK auto-downloads** into `~/.pyside6_android_deploy` (r27c) and the
   module scan / `cython` steps pass.
4. **Blocker:** buildozer then runs `pip install --user …` for the
   python-for-android build deps, which fails inside a virtualenv
   (`Can not perform a '--user' install. User site-packages are not visible in
   this virtualenv`). Buildozer must run against a **non-virtualenv** Python
   ≤3.11 (system/pyenv interpreter with a working `--user` site), after which it
   downloads the SDK (accepting licenses) and compiles CPython/numpy/pillow for
   Android — a multi-GB, long-running step best done on a dedicated build host
   or CI runner.

The scaffolding here (entry point, mobile requirements, spec) makes that build
reproducible once a compatible, non-virtualenv Python 3.11 host is available.
