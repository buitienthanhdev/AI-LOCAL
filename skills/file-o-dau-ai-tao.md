---
name: file-o-dau-ai-tao
triggers: o dau, nam o dau, tac gia, ai tao, ai viet, tao khi nao, tao luc, ngay tao, file nay, sua lan cuoi, do ai
---
Facts about a file (where / who / when).
1. Find its path: knowledge.query on visible_files (name LIKE ...), or filesystem.list / shell Get-ChildItem -Recurse -Filter.
2. file.info on that path: authors = the file's Authors property; first_seen = the day the file first appeared (this is 'ngay tao'); last_saved_by and fs_modified = last change.
3. If several files have the same name, list them all with their folders and ask which one.
