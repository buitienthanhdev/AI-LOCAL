---
name: office-microsoft-365
triggers: office, microsoft 365, m365, outlook, onenote, sharepoint, onedrive, teams, planner, power bi, power automate, power apps, visio, publisher, ms project, access, accdb
---
Microsoft Office / 365 work (spec: docs/specs/SPEC_OFFICE.txt).
- Do the work, do not just explain. Pick the method in this order: official API/connector (Graph) with granted rights > the app's object model/COM > a file-format library > UI Automation > screenshot+mouse (last resort). Check what is really there first (desktop vs web, local vs OneDrive/SharePoint, installed, signed in, licence); never assume Publisher, Access engine, Visio or Project exist.
- Files: confirm path and access, detect the real format (not just the extension), check size/integrity/password, work on a COPY, change only what was asked, then re-open the saved file and compare (rows/cols/totals, headings, tables, slide count). Scanned PDF/images need OCR. Encrypted or corrupt = say so, never fake it.
- Verify, never assume: formulas and error values (#REF! #VALUE! #DIV/0! #NAME?), empty vs 0 vs "", no formula overwritten by a value, no recalculation claimed without a calc engine; Word headings/TOC/tables/page numbers, export PDF and look at it; check EVERY slide for overflow/overlap.
- Safety levels: 1 read; 2 reversible edit on a copy; 3 affects others (send mail/message, share, invite, publish, cloud data) = confirm recipients, text and attachments first; an email draft is not permission to send; 4 bulk delete, permission change, untrusted macro/script = block or special approval. No tokens/passwords in logs; never run macros or attachments from untrusted sources; do not send internal data to a cloud model without permission.
- Ask (with options, multi-select where fitting, a default, and "Khac") when files share a name, template/range/recipient/overwrite is unclear; do not ask what the context already settles.
- Offline: say exactly which steps can continue; never pretend to send, sync or update the cloud.
