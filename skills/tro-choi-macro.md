---
name: tro-choi-macro
triggers: tro choi, game, ran san moi, ran san, macro, vba, nut bam, tu chay, chuyen dong, di chuyen, pixel, o pixel, ve bang o
---
Games, macros and "it must move" requests in Excel/Word.
- A static workbook is NOT a game. Never report "done" when the thing cannot move or react.
- Excel can only move things through a macro (VBA) or live formulas. Before writing any VBA, check ONCE: `Get-ItemProperty "HKCU:\Software\Microsoft\Office.0\Excel\Security" -Name AccessVBOM`. If it is 0 or missing, Excel blocks adding VBA from code. Do NOT change that setting and do not retry other ways to inject VBA.
- If blocked, say it plainly in ONE message and offer: (a) the user turns on File > Options > Trust Center > Macro Settings > "Trust access to the VBA project object model", then you inject the VBA; (b) a ready .bas file they import themselves (Alt+F11, File > Import); (c) the same game as a standalone HTML file (keyboard arrows, opens in the browser) which always works. Use task.ask_user for the choice.
- Pixel art in cells: set ColumnWidth about 2.5 and RowHeight about 15 so cells are square, then fill by RGB per cell from a small grid; verify by reading back a few cells.
