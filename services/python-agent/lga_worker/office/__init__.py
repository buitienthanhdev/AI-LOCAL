"""Office / Microsoft 365 agent modules (spec: docs/specs/SPEC_OFFICE.txt, sections 5-8, 11-14).

Pure-Python, offline-testable building blocks. Nothing here talks to a real Office application or to the network;
the COM handlers live in lga_worker/tools/office.py, the tool bindings of these modules in lga_worker/office/tools.py.

* filetype  - real format detection (magic bytes / package content), integrity flags, handler routing   (section 5)
* registry  - OfficeCapabilityRegistry with evidence-based health probes                              (section 7)
* guard     - OfficePermissionGuard: levels 1-4, ALLOW/CONFIRM/BLOCK, copy-before-edit, redaction     (section 11)
* workflow  - operation/workflow state machine, checkpoint/resume, retry policy, offline split       (sections 8, 12, 14)
* verify    - OfficeVerificationEngine: re-reads saved DOCX/XLSX/PPTX/PDF/CSV files and checks them     (sections 3, 15)
* audit     - JSONL audit log with provenance + learning-record schema                                (sections 11, 13)
* router    - OfficeIntentRouter: application + method by the spec priority, ambiguity -> question     (sections 2.1, 6, 10)
* graph     - MicrosoftGraphAdapter stub: reports available=False, never touches the network          (section 4)
"""

from __future__ import annotations

__all__ = ["audit", "filetype", "graph", "guard", "registry", "router", "verify", "workflow"]
