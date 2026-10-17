# Office / Microsoft 365 (spec: docs/specs/SPEC_OFFICE.txt)

## What exists

Pre-existing COM tools (`tools/office.json`, `lga_worker/tools/office.py`): `office.excel_read/write`, `office.word_read/write`,
`office.export_pdf`, `office.outlook_list/read/draft/send`. Also `document.read`, `file.info`, `data.aggregate`.

New, pure Python, offline-testable (`lga_worker/office/`, bound in `lga_worker/office/tools.py`):

| Module | Tool | Spec |
|---|---|---|
| `registry.py` OfficeCapabilityRegistry | `office.capabilities` | 7 |
| `filetype.py` real format detection | `office.detect` | 5 |
| `guard.py` OfficePermissionGuard, copy-before-edit, redaction | `office.check_action` | 11 |
| `router.py` OfficeIntentRouter + clarification questions | `office.route` | 2.1, 6, 10 |
| `workflow.py` state machine, checkpoint, retry, offline split | `office.workflow_plan`, `office.workflow_status`, `office.workflow_step` | 8, 12, 14 |
| `verify.py` OfficeVerificationEngine | `office.verify` | 3, 15 |
| `audit.py` audit log + learning records | `office.audit_tail`, `office.learn` | 11, 13 |
| `graph.py` Graph adapter STUB | (reported by `office.capabilities`) | 4 |

State files: audit and lessons in `logs/office/`, workflow checkpoints in `runtime/office/workflows/<id>.json`.

## How it behaves

* **Registry**: an operation is `supported` only if a tool implementing it is declared in `tools/*.json` AND the probe found its
  dependency (COM ProgID registered with its server file present, python module importable). Probes read the registry/import system only;
  nothing is started, so `verified_live` is always false. Installed apps without an adapter (Access, OneNote...) are `available=false`.
* **Detection**: magic bytes, zip content types, OLE2 directory parse (doc/xls/ppt/msg/vsd/pub/mpp heuristic, encrypted OOXML), PDF
  header/`%%EOF`/`/Encrypt`, Access ACE/Jet, text sniffing (csv/tsv/json/xml/html/eml/ics). Extension mismatch is a warning; the content wins.
* **Guard**: levels 1-4, ALLOW/CONFIRM/BLOCK. Confirmations are bound to a fingerprint of operation+params (changing recipients or files
  invalidates it). Executables/disguised programs, untrusted macros, disabling security warnings and sending internal data to a cloud
  model are BLOCKed. Unknown operations are level 3, never assumed safe.
* **Workflow**: SUCCEEDED requires evidence (otherwise UNKNOWN); timeout gives UNKNOWN; non-idempotent steps (send, create draft/record)
  are never retried until `reconcile` proves the action did not happen; a RUNNING step found in a checkpoint becomes UNKNOWN on load.
  Steps without registry evidence are marked unsupported and never runnable. Offline: network steps and their dependents "must wait";
  nothing is simulated.
* **Verification** (zip+XML, no Office, no openpyxl needed; pypdf for PDF): DOCX paragraphs/headings/tables/duplicates/images/placeholders,
  expected text, before/after loss; XLSX error values, `#REF!` in formulas, uncached formulas (reported, never recalculated), blank vs 0 vs
  "", key uniqueness, totals, before/after (formula overwritten by a value, row/col shrink, out-of-scope edits); PPTX every slide (bounds,
  overflow estimate, overlap, images, titles/order); PDF validity, pages, text layer, source text; CSV rectangularity. Verdict FAIL / WARN /
  PASS / NOT_RUN; things that cannot be known are listed in `not_verified`.
* **Learning**: only `verified` lessons with trusted provenance (tool_result, verification_engine, test_run, user_feedback, human_review...) and
  evidence; AI-generated sources are refused; secrets redacted; a lesson seen once is a candidate; `applied` is always false (no self-modifying
  production behaviour).
* Extra safety in COM code: apps started by the worker get `AutomationSecurity = ForceDisable` (macros of opened documents do not run).

## Honest limits / NOT implemented

* Microsoft Graph, OneDrive/SharePoint, Teams, Forms, Planner, Power Platform: no adapter, no OAuth. Stub reports `not configured/no credentials`.
* No tool for: Word TOC/track changes/headers, Excel pivots/VBA/Power Query, PowerPoint slide creation/editing, Outlook calendar/contacts/rules,
  OneNote, Access, Visio, Project, Publisher, PDF editing/OCR.
* Not live-tested: all COM paths (Word/Excel/PowerPoint/Outlook), the `AutomationSecurity` change, registry probes on machines with Office
  (probe code ran only on this machine). Page layout, TOC currency, fonts and real rendering cannot be verified from files.
* PPTX overflow is an estimate (font size x box size); placeholders inheriting geometry from the layout are not computed.
* The router is keyword based and may need a clarification question where a model would guess; it never guesses recipients or files.

## Tests

`services/python-agent/tests/test_office_spec_unit.py` (mock probes, synthetic files, no Office). Run:
`.venv/Scripts/python.exe -m pytest tests/test_office_spec_unit.py -q`. Contract lint: `cargo test -p lga-core --test tool_contract_lint`.
