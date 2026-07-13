"""Single source of truth for which build profile the app runs as.

The desktop build is the full application. The ``mobile`` profile (Android)
ships only the ShotSync "take a shooting for selection" flow and therefore
turns off the heavy, desktop-only subsystems (AI, XMP, batch utilities, the
filesystem browser, RAW/video decoding) and swaps the process-based decode
pools for threads, because Android does not support ``multiprocessing``.

The module deliberately depends on nothing but the standard library so it is
safe to import from anywhere, including ``version.py``-style isolated contexts.
"""

from __future__ import annotations

import os
import sys

DESKTOP = "desktop"
MOBILE = "mobile"


def _detect_profile() -> str:
    override = os.environ.get("RAWWW_PROFILE", "").strip().lower()
    if override in (DESKTOP, MOBILE):
        return override
    # python-for-android / Qt for Android set these at runtime.
    if os.environ.get("ANDROID_ARGUMENT") or os.environ.get("ANDROID_APP_PATH"):
        return MOBILE
    if sys.platform == "android" or hasattr(sys, "getandroidapilevel"):
        return MOBILE
    return DESKTOP


PROFILE = _detect_profile()
IS_MOBILE = PROFILE == MOBILE
IS_DESKTOP = not IS_MOBILE

# Feature switches. Desktop keeps everything; mobile keeps only what the
# selection workflow needs.
FEATURE_AI = IS_DESKTOP
FEATURE_XMP = IS_DESKTOP
FEATURE_UTILITIES = IS_DESKTOP
FEATURE_FILESYSTEM = IS_DESKTOP
FEATURE_RAW = IS_DESKTOP
FEATURE_VIDEO = IS_DESKTOP
FEATURE_SHOTSYNC = True
FEATURE_SHOTSYNC_RECEIVE = IS_DESKTOP
FEATURE_SHOTSYNC_UPLOAD = IS_DESKTOP

# Android has no usable ``multiprocessing``; decode on threads there instead.
DECODE_USE_PROCESSES = IS_DESKTOP
