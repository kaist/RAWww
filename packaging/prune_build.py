"""Post-build size reducer for the RAWww PyInstaller onedir bundle.

PyInstaller's Qt hooks copy plugins, translations and DLLs *defensively* — many
of which RAWww never loads. This script deletes the safe-to-remove ones and
prints a before/after size report.

Usage (from project root, after `pyinstaller packaging/rawww.spec`):

    python packaging/prune_build.py                 # prune dist/RAWww
    python packaging/prune_build.py --dist dist/RAWww
    python packaging/prune_build.py --dry-run       # show what WOULD be deleted
    python packaging/prune_build.py --report-only   # just measure, delete nothing

HOW TO ITERATE (this is the "manual pruning" loop the task asked for):
  1. Build once, run the app, confirm it works.
  2. Run this with --dry-run and read the candidate list.
  3. Move an entry from KEEP-thinking to the delete lists below, prune, retest.
  4. If something breaks, restore it here and rebuild/re-prune.

Everything is data-driven via the lists near the top so you can edit freely.
"""

from __future__ import annotations

import argparse
import fnmatch
import shutil
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Qt plugin *subfolders* to remove entirely. We keep only what a widgets +
# multimedia app needs: platforms, styles, imageformats, iconengines,
# multimedia, tls (https for media), and generic/platforminputcontexts.
# ---------------------------------------------------------------------------
QT_PLUGIN_DIRS_TO_DELETE = [
    "assetimporters",
    "designer",
    "geometryloaders",
    "networkinformation",   # keep if you later need network status
    "position",
    "qmltooling",
    "quick3d",
    "renderers",
    "renderplugins",
    "scenegraph",
    "sceneparsers",
    "sqldrivers",
    "texttospeech",
    "webview",
    "3dinputdevices",
]

# Individual glob patterns (relative to the bundle root) to delete.
FILE_GLOBS_TO_DELETE = [
    # Qt translations — RAWww ships its own strings.
    "**/PySide6/translations/*",
    "_internal/PySide6/translations/*",
    "**/translations/qt*_*.qm",
    # Qt QML runtime (excluded in spec, but hooks sometimes still copy it).
    "**/PySide6/qml/**",
    "_internal/PySide6/qml/**",
    # Big Qt DLLs for modules we excluded — deleted here as a safety net in
    # case a transitive hook re-added them.
    "**/Qt6Quick*.dll",
    "**/Qt6Qml*.dll",
    "**/Qt6WebEngine*.dll",
    "**/Qt6WebChannel*.dll",
    "**/Qt6WebSockets*.dll",
    "**/Qt6Charts*.dll",
    "**/Qt6DataVisualization*.dll",
    "**/Qt6Graphs*.dll",
    "**/Qt63D*.dll",
    "**/Qt6Pdf*.dll",
    "**/Qt6Designer*.dll",
    "**/Qt6Sql*.dll",
    "**/Qt6Test*.dll",
    "**/Qt6Bluetooth*.dll",
    "**/Qt6Nfc*.dll",
    "**/Qt6SerialPort*.dll",
    "**/Qt6Location*.dll",
    "**/Qt6Positioning*.dll",
    "**/Qt6TextToSpeech*.dll",
    "**/Qt6RemoteObjects*.dll",
    "**/opengl32sw.dll",       # software GL fallback (~15-20 MB); drop unless needed
    # ONNX Runtime GPU providers (we run CPUExecutionProvider only).
    "**/onnxruntime_providers_cuda*.dll",
    "**/onnxruntime_providers_tensorrt*.dll",
    "**/onnxruntime_providers_shared.dll",
    # Stray dev/test artifacts.
    "**/*.pdb",
    "**/*.lib",
    "**/*.exp",
    "**/api-ms-win-*.dll",     # usually redundant on modern Windows 10/11
]


def dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def human(n: int) -> str:
    x = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if x < 1024 or unit == "GB":
            return f"{x:.1f} {unit}"
        x /= 1024
    return f"{x:.1f} GB"


def find_bundle_root(dist: Path) -> Path:
    """Return the folder that actually contains the payload (handles _internal)."""
    internal = dist / "_internal"
    return internal if internal.is_dir() else dist


def collect_targets(dist: Path) -> tuple[list[Path], list[Path]]:
    root = find_bundle_root(dist)
    plugin_bases = list(dist.rglob("plugins")) + list(root.rglob("plugins"))
    dirs: list[Path] = []
    seen: set[Path] = set()

    for base in plugin_bases:
        for name in QT_PLUGIN_DIRS_TO_DELETE:
            candidate = base / name
            if candidate.is_dir() and candidate not in seen:
                dirs.append(candidate)
                seen.add(candidate)

    files: list[Path] = []
    fseen: set[Path] = set()
    for pattern in FILE_GLOBS_TO_DELETE:
        for match in dist.glob(pattern):
            if match.is_file() and match not in fseen:
                files.append(match)
                fseen.add(match)
            elif match.is_dir() and match not in seen:
                dirs.append(match)
                seen.add(match)
    return dirs, files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", default="dist/RAWww", help="Path to the onedir bundle")
    parser.add_argument("--dry-run", action="store_true", help="List targets, delete nothing")
    parser.add_argument("--report-only", action="store_true", help="Only print sizes")
    args = parser.parse_args()

    dist = Path(args.dist).resolve()
    if not dist.is_dir():
        print(f"error: bundle not found: {dist}", file=sys.stderr)
        print("Build first: pyinstaller packaging/rawww.spec --noconfirm --clean", file=sys.stderr)
        return 2

    before = dir_size(dist)
    print(f"Bundle: {dist}")
    print(f"Size before: {human(before)}")

    if args.report_only:
        # Show the 20 largest files to guide manual pruning decisions.
        files = sorted(
            (p for p in dist.rglob("*") if p.is_file()),
            key=lambda p: p.stat().st_size,
            reverse=True,
        )[:20]
        print("\nLargest files:")
        for p in files:
            print(f"  {human(p.stat().st_size):>10}  {p.relative_to(dist)}")
        return 0

    dirs, files = collect_targets(dist)
    freed = 0

    if not dirs and not files:
        print("Nothing to prune (already clean).")
        return 0

    print(f"\n{'Would delete' if args.dry_run else 'Deleting'} "
          f"{len(dirs)} folder(s) and {len(files)} file(s):")
    for d in dirs:
        size = dir_size(d)
        freed += size
        print(f"  [dir ] {human(size):>10}  {d.relative_to(dist)}")
        if not args.dry_run:
            shutil.rmtree(d, ignore_errors=True)
    for f in files:
        try:
            size = f.stat().st_size
        except OSError:
            size = 0
        freed += size
        print(f"  [file] {human(size):>10}  {f.relative_to(dist)}")
        if not args.dry_run:
            try:
                f.unlink()
            except OSError:
                pass

    print(f"\n{'Would free' if args.dry_run else 'Freed'}: {human(freed)}")
    if not args.dry_run:
        after = dir_size(dist)
        print(f"Size after:  {human(after)}  ({human(before - after)} smaller)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
