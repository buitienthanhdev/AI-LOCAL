---
name: pdf-tai-lieu
triggers: pdf, xuat pdf, doc pdf, trang, bao cao pdf, gop pdf
---
PDF files. Read: document.read (text; scanned pages are OCR'd and slow). Export a Word/Excel/PowerPoint file: office.export_pdf. Merge/split: python.exec with pypdf. After creating a PDF check that the file exists and starts with %PDF.
