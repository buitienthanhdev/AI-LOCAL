---
name: word-sua-tai-lieu
triggers: word, docx, van ban, thay chu, doi chu, xoa doan, them doan, soan thao, bao cao, hop dong
---
Editing or creating Word documents.
- Replace text everywhere: office.word_write with replace {old: new} (keeps formatting), or python-docx over paragraphs AND table cells AND runs.
- Delete a paragraph: python-docx paragraph._element.getparent().remove(paragraph._element).
- New document: python-docx; headings with doc.add_heading, bullets with style 'List Bullet', tables with doc.add_table.
- Save with the same name when asked to "sua file"; otherwise a new file next to the source.
Verify: re-read the file (office.word_read / document.read) and confirm the new text is present and the removed text is gone.
