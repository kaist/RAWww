# rawww

Python project managed with `uv`.

## Setup

```powershell
uv sync
```

## Run

```powershell
uv run rawww
```

## Viewer

- Grid mode: drive selector and directory tree on the left, adaptive 3:2 photo cards on the right.
- Full view: double-click a photo or press `F`.
- Back to grid: `Esc`, `Enter`, or `G`.
- Toggle fullscreen: `F11`.
- Navigate in full view: arrow keys or space.

JPEG files are decoded with draft downsampling for fast previews. RAW files use the embedded preview when available. Embedded ICC profiles are converted to sRGB before display.

The last opened folder is restored on startup.

The grid keeps its fast 256px JPEG cache. The currently open folder is watched for added, removed, renamed, and changed files and is refreshed after a short debounce. Unchanged cache and AI records are retained. EXIF batches share the background preview worker pool; every worker keeps one bundled ExifTool subprocess open instead of starting it per photo. CLIP embeddings and face detection/recognition start only when **Process new photos** is pressed and report progress in the sidebar. The button queues only new, changed, or previously unfinished photos. A 640px JPEG is prepared and shared by the two independent AI workers entirely in memory, then discarded; SQLite stores only the final embeddings and face data. The CLIP and InsightFace processes are created lazily for each manual run and terminated when its queue finishes, releasing their models from memory. ONNX models and the complete Windows ExifTool distribution live inside the application package, so no system installation or `PATH` configuration is required. Set `RAWWW_DISABLE_AI=1` before starting the app to disable background analysis.

Preview caches are kept centrally in the operating system's application-data directory, under `RAWww/cache/folder-caches`, with one SQLite file per browsed folder. The application writes only small JPEG grid previews, and entries are invalidated by file size and modification time. Existing larger preview variants are left untouched. SQLite is accessed directly on disk in WAL mode, so opening a folder does not duplicate its complete cache in application memory. Full-view images are decoded from their source files on demand and kept only in a bounded RAM LRU; up to ten neighbours in each direction are preloaded in the background.

Cache databases use a throughput-oriented SQLite profile: 32 KiB pages for large thumbnail records, WAL, a 128 MiB page cache, memory-mapped reads, and batched writes. Because the database is disposable, synchronous durability is disabled. Recent entries are regenerated after an abnormal shutdown; if a cache database itself is corrupt, it is deleted and rebuilt automatically from the source photos, including embeddings and face data.

The current full-view image uses a dedicated foreground decode pool and can duplicate an already-running background decode instead of waiting for it. In grid mode, the selected card starts a debounced foreground full-view decode so opening it can reuse the RAM result.

Folder opening is staged: the file list is populated in UI batches while the cache opens off the UI thread. Thumbnail work begins only after the cache is ready. The scheduler rebuilds its priorities after scrolling or resizing: visible cards are loaded from the viewport centre outward, followed by a one-screen buffer, and only then by the sequential background pass. Executor queues are intentionally short so work from an old viewport cannot build up after a fast scroll. Full-preview warming is limited to one frame at a time.

## Tests

```powershell
uv run python -m unittest discover -s tests -v
```

## Processing benchmark

Run per-stage preview, EXIF, CLIP, face-analysis, and SQLite measurements on the last folder opened in RAWww:

```powershell
uv run python -m rawww.ai_benchmark --limit 32
```

## Decode benchmark

```powershell
uv run python -m rawww.benchmark "D:\фото\на обработку\а ню" --limit 30 --full-limit 8 --full-size 2560
```
