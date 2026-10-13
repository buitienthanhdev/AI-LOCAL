---
name: excel-du-lieu
triggers: excel, xlsx, bang tinh, csv, tong theo, thong ke, pivot, loc du lieu, trung binh, tong hop
---
Spreadsheet / table data jobs.
1. Look first: office.excel_read (or python.exec with pandas/openpyxl, csv module) to see the columns and a few rows. Never guess column names.
2. Compute with python.exec, not in your head: groupby / sum / average / filter, print the result.
3. Write the result to the file the user named (new sheet or new file; never overwrite the source unless asked).
4. Verify: read the output back (office.excel_read or python.exec) and compare with the numbers you printed.
Traps: numbers stored as text (convert), header row counted as data, Vietnamese thousands separators (1.950.000 = 1950000), CSV encoding (use utf-8-sig).
