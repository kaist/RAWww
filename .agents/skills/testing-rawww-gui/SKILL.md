---
name: testing-rawww-gui
description: How to launch and test the «Контролька» (RAWww) PySide6 desktop app on a real X display, including folder-watcher / external-edit scenarios.
---

# Testing the RAWww («Контролька») desktop app

## Launch
- Real X display is required (`xcb` plugin); `DISPLAY=:0` is the desktop session on
  these boxes (`ls /tmp/.X11-unix` shows `X0`). `DISPLAY=:1` does not exist and the app
  will exit with "Could not load the Qt platform plugin xcb".
- Command: `cd <repo> && (DISPLAY=:0 uv run rawww "/path/to/folder" > /tmp/rawww.log 2>&1 &)`.
  Startup takes ~15 s. Maximize with
  `DISPLAY=:0 wmctrl -a Controlka; DISPLAY=:0 wmctrl -r Controlka -b add,maximized_vert,maximized_horz`.
- Never `pkill -f rawww` from the exec tool — the pattern matches your own shell command
  and kills the session. Use `pkill -f "uv run rawww"` from a different shell or `pkill -x rawww`.

## Settings gotcha (empty grid)
Settings live in `~/.config/ctrlka/ctrlka.conf`. Leftover keys from earlier sessions can
filter out every photo while the toolbar still looks default — most notably
`face_filter_embedding` / `face_filter_avatar` (status bar shows `-/0 (total N)`).
If the grid is empty but "total N" is non-zero, stop the app, `sed -i '/^face_filter/d'`
the conf (or move the whole file aside) and relaunch.

## Test images
Generate large solid-colour JPEGs with Pillow (`Image.new('RGB',(2000,1400),colour).save(...)`)
— easy to tell apart in a recording. Use a checkerboard/text image for the "edited" version.

## Folder watcher / external-edit scenarios
- The app watches the folder via `QFileSystemWatcher.directoryChanged`. On Linux this fires
  for create/delete/rename but **not** for in-place content rewrites of an existing file.
  To trigger the external-edit refresh path, write a temp file and `os.replace()` it over the
  target (this is what most real editors and exiftool do).
- If a plain in-place overwrite seems ignored, that may be the known watcher limitation, not
  your setup; verify by leaving the folder and returning (full rescan) — a workaround for the
  app could be watching individual files or polling size/mtime.
- Debounce: expect the refresh within ~2-6 s, not instantly.

## FullView
Double-click a card to open fullscreen; `Escape` returns to the grid.

## Devin Secrets Needed
None.
