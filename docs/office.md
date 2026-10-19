# Office / Microsoft 365 (spec: docs/specs/SPEC_OFFICE.txt)

## What exists

Pre-existing COM tools (`tools/office.json`, `lga_worker/tools/office.py`): `office.excel_read/write`, `office.word_read/write`,
`office.export_pdf`, `office.outlook_list/read/draft/send`. Also `document.read`, `file.info`, `data.aggregate`.

Extra COM tools (`lga_worker/tools/office_extra.py`, mixin of the `Office` class; every one reads back through COM AND from the saved file
and fails with `VERIFICATION_FAILED` when the result differs from the request; live-tested, see below):

| Tool | What it does | Read-back / verification |
|---|---|---|
| `office.word_toc` | insert/update a table of contents (levels, start/end), update all fields, save | TOC entries == document headings (a document without headings fails), `office.verify` docx |
| `office.word_review` | Track Changes on/off for an edit (`track`, `replace`), add a comment, list revisions + comments; accept/reject ALL only with explicit `resolve` | TrackRevisions state, new revisions exist (tracked) / none (untracked), comment text, `w:ins`/`w:del` and `comments.xml` in the saved file |
| `office.word_layout` | page numbers (PAGE field, footer or header, alignment), header/footer text, margins (cm), orientation, all sections | Word values + `sectPr`/`pgMar`/header/footer parts/PAGE field of the saved file |
| `office.excel_pivot` | create a PivotTable from a header+data range (rows, columns, sum/count/average/max/min) on a new sheet; `action: read` lists existing ones | grand total and, for one row field, every group recomputed in Python from the source; pivot part in the saved package |
| `office.excel_chart` | embedded chart (column/bar/line/pie/scatter/area) from a range | chart type, series, points per series (COM) and chart part kind/series/title (saved file) |
| `office.excel_format` | number format, conditional formatting (cell value / expression, fill and font colour), data validation (list/whole/decimal/date/text length/custom) | NumberFormat, rules and validation read from Excel; `cfRule`/`dataValidation` in the saved sheet XML |
| `office.ppt_create` | new deck with the default Office theme: title slide + content slides (bullets, notes); refuses to overwrite unless `overwrite` | PowerPoint read-back == saved file == request, `office.verify` pptx |
| `office.ppt_read` | slides, titles, bullets, speaker notes, layout (read only) | - |
| `office.ppt_edit` | add a slide, replace text on slides, set speaker notes | before/after read-back, saved file, `office.verify` pptx |

The registry reports `word.insert_toc`, `word.track_changes`, `word.headers_footers`, `excel.pivot_table`, `excel.chart`, `excel.format_cells`,
`powerpoint.create_slides`, `powerpoint.edit_slides`, `powerpoint.speaker_notes` as supported only when the tool is declared and the COM ProgID is registered.

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
* No tool for: Excel VBA/Power Query/PivotCharts, Word compare versions/styles, PowerPoint images/charts/themes/hyperlinks, Outlook calendar/contacts/rules,
  OneNote, Access, Visio, Project, Publisher, PDF editing/OCR.
* Live-tested (Word, Excel, PowerPoint on this machine, invisible, temp files in `artifacts/office-live/`): the nine extra tools above via
  `logs/dev/office_extra_live.py`. NOT live-tested: the older COM paths (Outlook, export_pdf), the `AutomationSecurity` change, other Office versions/languages.
* Extra-tool limits: `word_toc` scans at most 4000 paragraphs and lists headings by outline level; a TOC page number is not compared (pagination is Word's);
  `word_layout` replaces the whole header/footer content of every section when text or page numbers are given and does not handle different-first-page/odd-even;
  `word_review` accepts/rejects all revisions only (no per-revision choice), tracked FORMAT changes are listed but not compared with the saved XML;
  `excel_pivot` needs unique header names, compares cell values only without column fields, no PivotChart/slicers/calculated fields; `excel_chart` compares the
  expected series with a rule (numeric columns = series, text columns = categories) and has no axis/legend options; `excel_format` has no colour scales/data bars,
  the number format is compared as text and not against the saved style table; data validation is not exercised with invalid input;
  `ppt_create/ppt_edit` work with text, bullets and notes (default theme, layouts 'Title' and 'Title and Content'), replace does not look into tables/groups,
  PowerPoint may briefly show a window (it cannot be hidden); an already open document of the same path is edited in place and saved.
* PPTX overflow is an estimate (font size x box size); placeholders inheriting geometry from the layout are not computed.
* The router is keyword based and may need a clarification question where a model would guess; it never guesses recipients or files.

## Tests

`services/python-agent/tests/test_office_spec_unit.py` and `tests/test_office_extra_unit.py` (mock probes, fake Office, synthetic OOXML, no Office). Run:
`.venv/Scripts/python.exe -m pytest tests/test_office_spec_unit.py tests/test_office_extra_unit.py -q`. Live: `.venv/Scripts/python.exe ../../logs/dev/office_extra_live.py [word|excel|ppt]`. Contract lint: `cargo test -p lga-core --test tool_contract_lint`.
