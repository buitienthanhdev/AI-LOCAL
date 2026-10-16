---
name: excel-du-lieu
triggers: excel, xlsx, bang tinh, csv, tong theo, thong ke, pivot, loc du lieu, trung binh, tong hop, dong trong, dong trung, xoa dong
---
Spreadsheet / table data jobs.
1. Look first: office.excel_read (or python.exec with pandas/openpyxl, csv module) to see the columns and a few rows. Never guess column names.
2. Compute with python.exec, not in your head: groupby / sum / average / filter, print the result.
3. Write the result to the file the user named (new sheet or new file; never overwrite the source unless asked). When the user asks to change the file in place ("lưu lại cùng tên file"): FIRST copy the original to %TEMP%\lga-backup\<name> (shutil), then edit in ONE python.exec script, then read it back ONCE. Re-running a script on an already-edited file only wastes steps; if the readback is wrong, restore the backup and fix the script.
4. Verify: read the output back (office.excel_read or python.exec) and compare with the numbers you printed. When the readback shows exactly what the request asks for (e.g. no blank rows, no duplicates, header kept), call task.complete at once; do not run the same cleaning again.
Traps: numbers stored as text (convert), header row counted as data, Vietnamese thousands separators (1.950.000 = 1950000), CSV encoding (use utf-8-sig).
Do not change cell values you were not asked to change. pandas.read_excel turns text such as phone numbers "090" into the number 90.0 and writes it back that way: read with `pd.read_excel(path, dtype=str, keep_default_na=False)` (blank cells become "") or edit with openpyxl (ws.delete_rows) so every kept cell stays byte-identical. Blank row = every cell empty; duplicate row = same values in all columns (keep the first).
