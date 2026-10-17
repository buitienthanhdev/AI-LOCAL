"""MicrosoftGraphAdapter - an honest stub (spec sections 4, 6).

No OAuth flow, token store or HTTP client exists in this repository, so every Graph-backed capability is reported
unavailable. This module performs NO network calls and reads NO credentials; `status()` is constant on purpose.
Replace it with a real adapter only together with tests against a sandbox tenant.
"""

from __future__ import annotations

from typing import Any

REASON = "not configured/no credentials"


class GraphNotConfigured(RuntimeError):
    """Any attempt to use Graph before an adapter exists."""


class GraphAdapter:
    adapter_id = "microsoft_graph"

    def status(self) -> dict[str, Any]:
        return {"adapter": self.adapter_id, "available": False, "authenticated": False, "reason": REASON,
                "network_calls_made": 0, "scopes_granted": [],
                "note": "stub: no OAuth, no HTTP client; Outlook/OneDrive/SharePoint/Teams cloud operations are unsupported"}

    def call(self, *_args: Any, **_kwargs: Any) -> Any:
        raise GraphNotConfigured(f"Microsoft Graph is unavailable: {REASON}")

    # Named entry points so callers get one clear error instead of AttributeError.
    list_mail = send_mail = list_files = upload = share = call
