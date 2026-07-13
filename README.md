# Контролька

Python project managed with `uv`.

## Setup

```powershell
uv sync
```

## Run

```powershell
uv run rawww
```

The application also accepts a file or folder path:

```powershell
uv run rawww "D:\photos\shoot"
uv run rawww "D:\photos\shoot\IMG_0001.CR3"
```

To diagnose a startup flash on Windows, record every native window created
during the first three seconds:

```powershell
$env:RAWWW_TRACE_STARTUP = "1"
uv run rawww 2> startup-windows.log
```

A folder is opened in its own workspace tab. A file opens its parent folder
and immediately switches to Full View for that file; it is presented directly
fullscreen, without first showing the grid or restored workspaces. This is the
same command-line contract used by file-manager integrations.

## Viewer

- Folder workspaces open in independent tabs. Use `+` to create a tab; tabs may be reordered or closed without changing another tab's current folder, filters, or selection. Open tabs and the active tab are restored at startup; tabs whose folders no longer exist are omitted.
- Grid mode: mounted disks appear as buttons above the directory tree on the left; the list refreshes automatically, includes USB media and inserted cards, and excludes empty card-reader slots.
- ShotSync-style selection toolbar: filter by rating, color label, shot size, filename/comment, and change the sort order or card size. The **Серии** grouping toggle is global: its state applies immediately to every open tab and remains the same when another folder is opened.
- In the ShotSync panel, shooting cards open their local folder when one exists; cloud-only shootings offer **Take for selection** or **Watch**. Sending a shooting opens a dialog to choose its name, source folder, and server-side AI face/series processing.
- Use Ctrl/Shift to select multiple cards, then assign a 0-5 rating, color label, comment, or the 5-star quick mark. Selection metadata is stored in the central folder cache and survives restarts.
- Drag files and folders from the grid to a folder card, the folder tree, or another workspace tab to move them; hold `Ctrl` while dragging to copy. Dragging from Explorer into the grid, tree, or a tab copies the dropped items into that folder. The grid also exposes selected items as regular file URLs, so they can be dragged out to Explorer or another application.
- In the grid or focused folder tree, `Ctrl+C`, `Ctrl+X`, and `Ctrl+V` copy, cut, and paste selected files/folders. `Ctrl+D` clears the current selection. Existing names are never overwritten.
- `Shift+C` and `Shift+M` open the quick copy/move destination picker. Repeat the shortcut or press Enter to use the selected path; `1`–`9` starts the operation for that numbered path immediately. The last used destination and open workspace folders are offered automatically; shortcuts can be changed in **Settings → Hotkeys**.
- In the grid or focused folder tree, `Del` moves selected files/folders to the system recycle bin; `Shift+Del` removes them permanently. Confirmation is enabled by default and can be disabled in **Settings → Behaviour**. Individual photos from a ShotSync selection copy are protected from deletion.
- Right-click a folder in the tree and choose **Rename** to edit its name inline. Its preview and AI cache is moved to the new path, including caches for nested folders.
- Select a processed photo and press **Find face** to show photos containing a matching face; the × button clears face search. **No faces** is available in the shot-size filter.
- Hotkeys: `1`-`5` assign a rating, `0` clears it; `Shift+1`-`5` set red through purple colour labels and `Shift+0` clears the label. `M` toggles the quick mark, `C` opens the comment editor, and `E` opens the active file in the external editor. By default this is Adobe Photoshop; set another executable in **Settings → Behaviour**. Change hotkeys in **Settings → Hotkeys**; arrow keys, Enter and Esc remain reserved for navigation.
- **Коды замены** are configured in **Настройки → Коды замен**. They work locally without an account; sign in through the shared ShotSync login dialog to use the account's synchronized sets. Create sets, edit codes, or import CSV/TSV/XLSX. A completed code/value row is persisted when **Готово** is pressed even if Enter or Tab was not used first. In every comment field type `{`, `\`, `=` or `@` to open and filter code suggestions; Контролька stores the marker (for example `{name}`) and shows its expanded value directly in the field when it is not being edited.
- The **Утилиты** button holds batch tools that run across multiple processes: rename, **Групповой резайс** (export the current list to JPEG), and **Уменьшить JPG** (re-encode every JPEG in the open folder in place at a chosen quality, default 85%, overwriting without confirmation and reporting the megabytes saved). Both export tools always keep the embedded ICC profile and keep full EXIF (including sub-IFDs) when **Сохранить EXIF** is ticked. EXIF/ICC is only written by these export tools; the preview pipeline never embeds metadata.
- The **XMP** button beside AI creates Lightroom/Capture One-compatible `.xmp` sidecars. It exports ratings, colour labels, expanded comments, hashtags as keywords, and named face regions. Enable automatic creation to update sidecars in the background after local or ShotSync metadata changes.
- Full view: double-click a photo or press `F`. Hold the left mouse button for a temporary 100% inspection and drag to pan; press `Z` to toggle it. The inspector decodes and caches the full JPEG or the embedded RAW preview (falling back to RAW decoding only when needed), focusing the largest recognized face when available (otherwise the cursor position or image centre); arrow keys pan by 5% while it is active. Face focus on zoom can be turned off in **Settings → Interface** (**Акцент на лице при зуме**, on by default), after which zoom always centres on the cursor.
- Full view shows the current rating and colour label as a floating badge on the right; click an empty badge to apply the quick mark (`M`), or a marked badge to clear all marks. Choose its top/bottom position or disable it in **Settings → Interface**.
- Back to grid: `Esc`, `Enter`, or `G`.
- Toggle fullscreen: `F11`.
- Navigate in full view: arrow keys or space.
- Collapse the full-view lower panel with `Shift+Down`: the first press hides the thumbnail strip, the second hides the metadata/marks bar too. `Shift+Up` reverses it step by step. The state is remembered between sessions and both shortcuts can be reassigned in **Settings → Hotkeys**.
- Video files (`.mp4`, `.mov`, `.m4v`, `.avi`, `.mkv`, `.webm`) show a captured frame and video badge in the grid. In full view, use Play/Pause and the seek bar; playback uses Qt Multimedia and the system media codecs.
- Camera WAV notes matching a photo's filename are available for playback when a folder opens. The microphone badge opens the audio player in full view, and hovering the microphone badge in the grid plays the matching WAV without a transcript popup.

JPEG files are decoded with draft downsampling for fast previews. RAW files use the embedded preview when available. Embedded ICC profiles are converted to sRGB before display.

The last opened folder is restored on startup.

The grid keeps its fast 256px JPEG cache. The currently open folder is watched for added, removed, renamed, and changed files and is refreshed after a short debounce. Unchanged cache and AI records are retained. EXIF uses one dedicated process with one bundled stay-open ExifTool subprocess, so metadata never occupies thumbnail or full-preview decode workers. CLIP embeddings and face detection/recognition start only when **Process new photos** is pressed and report progress in the toolbar status panel, Windows taskbar, and as a percentage badge in the macOS Dock. The button queues only new, changed, or previously unfinished photos. Enable **Всегда запускать AI после превью** in **Settings → Behavior** (off by default) to start this analysis automatically once a folder's previews are ready; it also fires after new photos are added and re-previewed. AI waits until generated previews and cached previews being read from SQLite have actually reached the grid. Cache checks, source preparation, CLIP, and face recognition then run in dedicated low-priority background queues with an independent cache connection, so changing folders or tabs does not cancel the task or block navigation. A 640px JPEG is prepared and shared by the two independent AI workers entirely in memory, then discarded; SQLite stores only the final embeddings and face data. The CLIP and InsightFace processes are created lazily for each run and terminated when the queue finishes, releasing their models from memory. ONNX models and the complete Windows ExifTool distribution live inside the application package, so no system installation or `PATH` configuration is required. Set `RAWWW_DISABLE_AI=1` before starting the app to disable background analysis.

Preview caches are kept centrally in the operating system's application-data directory, under `Контролька/cache/folder-caches`, with one SQLite file per browsed folder. The application writes only small JPEG grid previews, and entries are invalidated by file size and modification time. Existing larger preview variants are left untouched. SQLite is accessed directly on disk in WAL mode, so opening a folder does not duplicate its complete cache in application memory. Full-view images are decoded from their source files on demand and kept only in a bounded RAM LRU; up to ten neighbours in each direction are preloaded in the background.

Cache databases use a throughput-oriented SQLite profile: 32 KiB pages for large thumbnail records, WAL, a 128 MiB page cache, memory-mapped reads, and batched writes. Because the database is disposable, synchronous durability is disabled. Recent entries are regenerated after an abnormal shutdown; if a cache database itself is corrupt, it is deleted and rebuilt automatically from the source photos, including embeddings and face data. Shortly after startup, a low-priority task removes caches whose source folders no longer exist and compacts caches with records for deleted files.

The current full-view image uses a dedicated foreground decode pool and can duplicate an already-running background decode instead of waiting for it. In grid mode, the selected card starts a debounced foreground full-view decode so opening it can reuse the RAM result.

Folder opening is staged: the file list is populated in UI batches while the cache opens off the UI thread. Thumbnail work begins only after the cache is ready. The scheduler rebuilds its priorities after scrolling or resizing: visible cards are loaded from the viewport centre outward, followed by a one-screen buffer, and only then by the sequential background pass. Executor queues are intentionally short so work from an old viewport cannot build up after a fast scroll. Full-preview warming is limited to one frame at a time.

## Tests

```powershell
uv run python -m unittest discover -s tests -v
```

## Updates

The application version is generated as `1.0.<Git commit count>` during build
and is shared with the running application through `src/rawww/version.py`.
Контролька checks
`https://shotsync.ru/ctrlka/api/version/` ten seconds after launch by default;
this can be disabled in **Settings → About**. Published releases and their
platform-specific builds are managed in the ShotSync admin panel.

## Windows build

Create a diagnostic `onedir` build and print its largest bundled files:

```powershell
uv run python scripts/build_pyinstaller.py
```

For an experimental smaller build, additionally compress native binaries with
UPX:

```powershell
uv run python scripts/build_pyinstaller.py --upx
```

The executable is created at `dist/ctrlka/ctrlka.exe`. Keep the complete
directory when distributing it. Supporting libraries are in `bin`; application
models, assets, and ExifTool are in `data`. The build script prints the largest files and
automatically removes unused Qt QML/Quick/PDF/OpenGL DLLs and keeps only the
Russian Qt base translation after every build; `onedir`
is also the format used to inspect and safely reduce bundled data before making
an optional `onefile` release.

GitHub Actions builds the Windows `onedir` package as
`ctrlka-windows-portable.zip` and the Russian Inno Setup installer on every
push or when started manually. Both files are published as the
`rawww-windows` workflow artifact.

The portable build keeps its settings, cache, and working data in the `work`
folder beside `ctrlka.exe`; the installer build uses the normal Windows data
locations.

## File-manager integration

All integrations should invoke the executable with a quoted path argument:
`".../ctrlka.exe" "%1"`. The application supports both files and folders.

- Windows: register a per-user file association for the desired RAW/image
  extensions and a folder context-menu verb under `HKCU\Software\Classes`.
  This does not require administrator privileges. The command value is
  `"C:\path\to\ctrlka.exe" "%1"`.
- macOS: package the application as `.app`, declare supported image UTTypes in
  `CFBundleDocumentTypes`, and handle the launch/open-file event by forwarding
  its path to this same entry point.
- Linux: install a `.desktop` file with `Exec=ctrlka %f` and the image MIME
  types; add a folder action with `Exec=ctrlka %d` where the desktop supports
  it.

For Windows, Explorer can only send new activation requests to an already
running instance after a later single-instance/IPC layer is added. Until then,
each Explorer action starts a separate application window, which is safe and
still opens the requested target.

In the packaged Windows application, use **Settings → Behaviour → Explorer
integration** to add or remove these commands. The app registers its own
`ctrlka.exe`; the change is applied immediately for the current user.

The equivalent command-line helper remains available:

```powershell
uv run python scripts/register_windows_integration.py
```

The script writes only to the current user's registry, does not replace the
default image viewer, and adds **Open in Контролька** for supported media and
folders. To remove the commands later, run:

```powershell
uv run python scripts/register_windows_integration.py --unregister
```

## Processing benchmark

Run per-stage preview, EXIF, CLIP, face-analysis, and SQLite measurements on the last folder opened in Контролька:

```powershell
uv run python -m rawww.ai_benchmark --limit 32
```

## Decode benchmark

The output compares thumbnail creation with and without blocking EXIF extraction; SQLite thumbnail hits never invoke ExifTool.

```powershell
uv run python -m rawww.benchmark "D:\фото\на обработку\а ню" --limit 30 --full-limit 8 --full-size 2560
```
