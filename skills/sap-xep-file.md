---
name: sap-xep-file
triggers: sap xep, don dep, di chuyen, doi ten, thu muc, trung nhau, thu muc rong, luu tru, phan loai, dua ve
---
Organising files and folders.
1. List first (filesystem.list or shell Get-ChildItem -Recurse) and decide the rule in one sentence.
2. Do the whole batch in ONE shell.exec / python.exec script (create the target folders, move, print what moved).
3. Same content = same SHA256 (Get-FileHash); keep one copy. Empty folders: remove only those with no files at any depth.
4. "Old" files: compare LastWriteTime with the cut-off date; move, never delete unless the user said delete.
5. Verify by listing again. Never touch files outside the folder the user named.
