## 2025-02-27 - [Add scroll to variable-length controls]
**Learning:** In Kodi XML skins (10-foot UIs), variable-length text fields (such as codec, audio, indexer, and release group) within `<focusedlayout>` and `<itemlayout>` will permanently truncate and hide important information if they exceed their `<width>`.
**Action:** Always add `<scroll>true</scroll>` to variable-length text `<label>` controls to ensure all metadata is accessible to the user when focused.
