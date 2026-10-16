---
name: office-quy-trinh-lien-ung-dung
triggers: office, bao cao tuan, quy trinh, workflow, lien ung dung, email nhap, tong hop, trich xuat, dinh kem
---
Office cross-application workflows (spec: docs/specs/SPEC_OFFICE.txt, sections 7-8, 12-15).
- Plan as steps with dependencies, a status per step, checkpoints and the ability to resume from the finished step. Statuses: PENDING, RUNNING, SUCCEEDED, FAILED, PARTIAL, WAITING_FOR_USER, CANCELLED, UNKNOWN (use UNKNOWN when there is no evidence either way).
- Typical chain: gather sources > cross-check and de-duplicate > DOCX from template > XLSX summary > PPTX if asked > export PDF > check content AND layout > email DRAFT with attachments > wait for the user's confirmation before sending.
- Keep facts (with source file/page/mail) apart from inference. Report what was done, what remains, and what changed outside scope (must be nothing).
- Recovery: retry only transient, safe operations; never blindly retry sending mail or creating records (duplicates); after a timeout check the real state first; locked workbook = wait with a limit or work on a copy; if saving failed do not say it was saved; for a partly finished workflow name the finished and unfinished steps.
- Learn from results only: record task_type, app, failure, root cause, fix, verification, confidence, source; never treat the AI's own answer as proof of success; do not store secrets from documents; do not repeat a failed strategy unchanged.
- PASS needs: run in the stated environment, output exists and opens, key data matches the source, no out-of-scope change, a reproducible test and evidence. Do not mark PASS because no error appeared, and do not claim "all of Office" when only some apps work.
