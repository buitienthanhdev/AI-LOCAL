---
name: viet-code
triggers: python, script, code, lap trinh, chay thu, sua loi, bug, viet ham, regex, sql, batch, powershell
---
Writing or fixing code.
1. Write the file (filesystem.write) in the folder the user named, then RUN it (shell.exec / python.exec) and read the output.
2. A syntax error or traceback: read the exact line, fix that, run again. Do not rewrite everything.
3. Report what the program printed, not what you expect it to print.
4. Do not run destructive scripts (delete, format, copy over data) unless the user asked you to run them.
