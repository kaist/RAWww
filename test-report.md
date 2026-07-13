# Test report — PR #35 (Full view Shift+Down/Up + mark-badge fix)

**How tested:** Ran the dev build (`uv run rawww`) with the PR branch against `/home/ubuntu/rawww_test` (5 photos) and exercised the feature entirely through the UI. Automated suite: 91 passed.

**Result:** All assertions passed. No blockers.

## Assertions

- ✅ **1st Shift+Down** in full view hides the thumbnail strip; marks bar stays visible.
- ✅ **2nd Shift+Down** hides the entire bottom panel (marks bar gone too).
- ✅ **1st Shift+Up** restores the marks bar without the thumbnail strip.
- ✅ **2nd Shift+Up** restores the thumbnail strip (full state).
- ✅ **Round mark badge** re-pins to the new bottom-right edge when the panel collapses, and returns above the marks bar when restored (the reported bug).
- ✅ **Persistence**: after collapsing fully, leaving full view and re-entering keeps the panel hidden.
- ✅ **Help → Горячие клавиши** lists Свернуть = `Shift+Down`, Развернуть = `Shift+Up`.
- ✅ **Settings → Горячие клавиши** exposes both as editable rows (Shift+Вниз / Shift+Вверх).
- ✅ **Regression**: grid `Shift+Arrow` still extends selection (hotkey is scoped to full view, so it no longer shadows grid multi-select).

## Evidence

### Full view — precondition (level 0)
Marks bar + thumbnail strip + round badge all visible.

![precondition](/home/ubuntu/screenshots/ss_7e7f54a9.png)

### Shift+Down step 1 — thumbnail strip hidden, marks bar stays
![step1](/home/ubuntu/screenshots/ss_zoom_ec93e5ed.png)

### Shift+Down step 2 — whole bottom panel hidden, badge re-pinned to new bottom edge
![step2](/home/ubuntu/screenshots/ss_zoom_bad0dd36.png)

### Persistence — re-entering full view keeps panel hidden
![persist](/home/ubuntu/screenshots/ss_zoom_890d523d.png)

### Help dialog — Shift+Down / Shift+Up
![help](/home/ubuntu/screenshots/ss_zoom_26461934.png)

### Settings → Hotkeys — editable Shift entries
![settings](/home/ubuntu/screenshots/ss_zoom_a03ce3ea.png)

### Regression — grid Shift+Right still multi-selects ("выделено: 2")
![regression](/home/ubuntu/screenshots/ss_zoom_0976ca97.png)
