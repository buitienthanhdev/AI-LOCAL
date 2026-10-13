---
name: powerpoint-trinh-chieu
triggers: powerpoint, pptx, slide, trinh chieu, thuyet trinh
---
PowerPoint files: python.exec with python-pptx.
- Slide with title + bullets: layout 1 (prs.slide_layouts[1]); title = slide.shapes.title, bullets = slide.placeholders[1].text_frame.
- One slide per requested title, in order; no extra blank slide.
- Verify by reopening the file and listing slide titles.
