# Agent isolation: own browser now, own Windows desktop (Hyper-V VM) next

Status 2026-10-22. Step 1 (browser) is done and tested. Step 2 (VM) is built and tested up to the VM
boundary. Creating the VM needs the owner's approval: see "Approvals needed".

## 1. Audit of the architecture before this change (read-only)

| Area | Before | Problem for "AI works next to me" |
|---|---|---|
| Browser | Own Chrome (`runtime/chrome-profile`), CDP + browser-use, DOM-level actions | Already isolated from the user's Chrome. No visible state, no Stop button in the page, and the user could click into the agent's tab |
| Desktop apps (Notepad, Paint, Word UI, AutoCAD UI) | Real `SendInput` mouse/keyboard on the user's desktop (UIA, Cua Driver since 2026-10-22) | Takes the user's mouse, keyboard and foreground. The core pauses on user activity (`pause_on_activity`) and shows a stop ribbon. No concurrency is possible |
| Office/AutoCAD automation | COM (`office.*`, `autocad.*`) | Does not need input, but runs in the user's session and opens windows there |
| Stop | `memory/tasks/<id>.cancel` marker, polled every 500 ms. The in-flight worker call is cancelled | Bug found and fixed: Stop during a planner call ended the task as `failed` ("internal error") instead of `cancelled` |
| Worker | One Python process per agent run, JSON lines over stdio | Fits remoting: the same protocol can cross into a VM |

## 2. Step 1: Browser Agent (done)

* `lga_worker/browser/overlay.py`: `OverlayController` owns its own CDP websocket to the agent's Chrome.
  It attaches to every page target (existing tabs, new tabs, popups, tabs that appear after a crash/relaunch).
  It injects the overlay with `Page.addScriptToEvaluateOnNewDocument`, so the overlay survives navigation and
  reload. The overlay has:
  * a blue frame;
  * the pill "Agent đang chạy – đã khoá điều khiển";
  * a progress line read from `memory/tasks/<id>.json` (step, tool, thought);
  * a Stop button.
* Input shield: a full-page DOM layer plus capture-phase listeners for key, wheel, clipboard and drop
  events. It is lowered only while an agent action runs (`agent_input()` around every `attached()` call).
  CDP-dispatched input goes through the same hit-testing as a real mouse. The probe showed that
  `Input.setIgnoreInputEvents` would also block the agent itself, so it is not used.
* Stop (button → CDP binding `__lgaAgentStop`), in order:
  1. writes the cancel marker (the core cancels the task and its in-flight worker request);
  2. cancels this worker's running browser calls at once;
  3. runs `Page.stopLoading` on every tab;
  4. lowers the shield;
  5. refuses every later browser call of that task (`CANCELLED`).

  The overlay is removed when the task reaches a terminal state.
* The overlay is excluded from browser-use's element list (`data-browser-use-exclude`). It is not part of
  `body.innerText`.
* It is UX only, NOT security: page scripts can see or remove it, and the Chrome frame (tabs, address bar)
  still accepts input. Isolation from the user's browsing comes from the separate Chrome process and profile.
* Planner rule 5: web pages are operated with `browser.*`, never with `desktop.*` clicks.
* Config: `[browser] overlay = true`.

Tests:
* `tests/test_browser_overlay_unit.py` (12).
* `tests/test_browser_overlay_live.py` (9, real Chrome). Covers injection, shield vs. agent click, navigation,
  reload, new tab, popup, timeout, Chrome crash + relaunch, Stop with an in-flight call, a real click on Stop,
  removal at task end, and the user's other Chrome processes being untouched.
* `tests/test_browser_stop_e2e.py`: a real `agent run`, then Stop clicked on the overlay. The task ended
  `cancelled` 0.4 s after the click.

## 3. Step 2: isolated desktop. Options studied

| Option | Own desktop/input | Persistent state | GUI apps | Runs next to the user | Verdict |
|---|---|---|---|---|---|
| **Hyper-V VM (Gen 2)** | Yes: separate virtual mouse, keyboard, display, clipboard | Yes: VHDX, checkpoints, Save/Suspend | Yes, full Windows | Yes | **Chosen** |
| Windows Sandbox | Yes | No: everything is discarded on close; one instance; no checkpoints | Yes | Yes | Rejected: loses the agent's installed apps and state |
| Second Windows session (another user via RDP loopback) | Yes | Yes | Yes | **No**: Windows 11 Pro allows one interactive session; connecting disconnects the console. Only patched termsrv (RDPWrap) avoids that, which breaks the license | Rejected |
| Same desktop, window lock | No | n/a | n/a | No | Rejected by the owner |

### This PC (read with `scripts/vm/check-host.ps1`, read-only)

* Windows 11 Pro 26200: Hyper-V is allowed on this edition.
* 63.4 GB RAM (about 25 GB free while the owner works). Intel Core Ultra 7 265K, 20 cores.
* D: has 299 GB free (VM disk goes there; C: has 54 GB).
* A hypervisor is already running (VBS status 2). That is why the firmware VT flag reads False; it is not a
  blocker.
* The Hyper-V role is NOT enabled: there is no `vmms` service and no Hyper-V PowerShell module.
* No Windows ISO was found.
* The agent's Python is 3.12.13 and has `AF_HYPERV`.

### Licensing (owner must check)

* The guest Windows needs its own license, or a Windows 11 Enterprise evaluation (90 days, for testing).
  The host's Pro license does not cover a second installation.
* Paint and Notepad are part of Windows: no extra cost.
* Microsoft 365 apps in the VM count as one of the user's device installs.
* AutoCAD in the VM depends on the Autodesk subscription terms (named user: usually fine, one active session).

## 4. Step 2 architecture

```
 host (your desktop)                                    agent VM "LGA-Agent" (no network adapter)
 ┌────────────────────────────┐                         ┌──────────────────────────────────────┐
 │ chat window / agent core   │                         │ auto-logon user lga-agent (non-admin)│
 │   │ JSON lines (stdio)     │                         │ scheduled task "LGA guest worker"    │
 │ host worker (Python)       │  Hyper-V socket (VMBus) │ python -m lga_worker.remote.guest    │
 │   VmRouter ── desktop.*, ──┼──── HMAC-auth channel ──┼─▶ same Worker + tools (UIA, Cua,     │
 │   paint.*, art.*, vision.* │   (no IP network)       │   Paint, Office COM, AutoCAD COM)    │
 │   browser.* stays on host  │                         │ C:\LGA\runtime\vm-workspace          │
 │ runtime/vm-workspace  ◀────┼── audited file copies ──┼─▶                                    │
 └────────────────────────────┘                         └──────────────────────────────────────┘
        vmconnect (basic session) = viewer + manual take-over; vm-control.ps1 pause/resume/revert
```

* Transport: Hyper-V socket `hv:<vm-guid>:<service-guid>`. The VM has no NIC, so it cannot reach the LAN,
  the host network or the internet. The host side registers the service GUID once
  (`new-agent-vm.ps1`). `tcp:` exists for tests only.
* Authentication (`lga_worker/remote/channel.py`):
  * mutual HMAC-SHA256 challenge/response with a 32-byte pre-shared key (`runtime/vm/agent-vm.key`, ACL'd);
  * then a session key, with every frame MAC'd and sequence-numbered (no forgery, replay, reorder or
    reflection);
  * frames are limited to 48 MB.
* Routing (`lga_worker/remote/host.py`): `[vm] tools` (default `desktop.*`, `paint.*`, `art.paint`,
  `art.draw`, `vision.*`) are forwarded. The core and planner see the same tools and errors. When the VM is
  on, the core shows no stop ribbon and does not pause on the user's activity (`gui_in_vm`), so both can
  work at once.
* Files (`lga_worker/remote/workspace.py`):
  * only `runtime/vm-workspace` on the host is shared, by copying with a SHA-256 check;
  * paths are guarded: no absolute, UNC, `..`, ADS, device names, or junction escapes;
  * every transfer and refusal goes to `logs/vm/host-audit.jsonl` (guest: `guest-audit.jsonl`);
  * any other host path in a tool argument is refused with POLICY_DENIED;
  * artifacts (screenshots, saved files) are copied back to `artifacts/vm/<task>/`.
* Failure handling:
  * VM down or not ready → BACKEND_UNAVAILABLE (retryable), with reconnect backoff;
  * connection lost mid-action → retryable "may or may not have happened, observe first";
  * Stop → cancel forwarded, and the guest's CANCELLED is awaited (≤5 s);
  * deadline → the guest enforces it, and the host adds 5 s of grace then cancels;
  * host disconnect → the guest cancels everything and closes its providers (keys and buttons released);
  * VM paused → TIMEOUT, then recovery after resume.
* Viewer / take-over: `scripts/vm/vm-control.ps1 view` opens vmconnect in a basic session (enhanced session
  is disabled, so the clipboard and drives are not redirected). Click into it to take over. `pause` freezes
  the whole VM instantly; `revert` goes back to the clean checkpoint. Status and log are in the chat window
  (task steps) and in `logs/vm/*.jsonl`.
* Structured automation first: `office.*` and `autocad.*` COM tools run inside the VM like on the host once
  those apps are installed there. GUI automation is used only where no API exists.

### What is implemented and tested now (no VM needed)

* `tests/test_vm_remote_unit.py` (20): handshake, wrong key, tampering, replay, reflection, path guard
  including a real junction escape, checksum, audit, routing, refusal of host paths.
* `tests/test_vm_remote_integration.py` (2): a real guest worker process (the code that runs in the VM) on
  loopback, driven by the host router. Covers:
  * a shared-file copy and path rewrite;
  * refusal of a host path;
  * a screenshot artifact fetched back;
  * Stop of a 60 s call (confirmed in under 6 s);
  * a guest deadline;
  * the guest killed mid-call (retryable error, then automatic reconnect);
  * a wrong key rejected.
* `tests/test_vm_e2e.py` (marker `vm`), ready for the VM:
  * Notepad typing and Paint drawing in the VM, with assertions that the host foreground window, cursor
    position and window set do not change;
  * Stop during `paint.draw`;
  * Suspend-VM during a call, then recovery.

## 5. Migration plan

| Step | What | Who / approval |
|---|---|---|
| H0 | `scripts/vm/check-host.ps1` (read-only) | done |
| H1 | `scripts/vm/enable-hyperv.ps1` (admin, then reboot) | **owner approval** |
| H2 | Owner provides a Windows 11 ISO and license (or an Enterprise evaluation ISO). `scripts/vm/new-agent-vm.ps1 -IsoPath …` creates LGA-Agent with no NIC, vTPM, Secure Boot, enhanced session off, the hv-socket service and the key | **owner approval** |
| H3 | Install Windows in vmconnect (owner, about 20 min). `scripts/vm/provision-guest.ps1 -GuestAdmin (Get-Credential)` installs the guest worker over PowerShell Direct and takes the "clean-provisioned" checkpoint. Needs temporary internet once for `uv sync` (`new-agent-vm.ps1 -TemporaryInternet`, removed afterwards) | **owner approval** |
| H4 | Optional: install Office/AutoCAD inside the VM (licenses) | **owner approval** |
| H5 | `configs/config.toml`: `[vm] enabled = true`, `endpoint = "hv:<vm-guid>:<service-guid>"`. Restart the chat window | owner / me |
| H6 | `pytest --run-vm tests/test_vm_e2e.py`, then real chat tasks (Paint, Notepad) while the owner works | me |
| Rollback | `[vm] enabled = false` returns to the host desktop. `vm-control.ps1 revert` resets the VM. `Remove-VM LGA-Agent` plus `Disable-WindowsOptionalFeature Microsoft-Hyper-V-All` removes everything | owner |

### PoC gate before moving more tools into the VM

The VM removes input conflicts. It does not by itself make AutoCAD, GPU-heavy apps or Office automation
work. So `[vm] tools` starts with `desktop.*`, `paint.*`, `art.*` and `vision.*` only. `office.*` and
`autocad.*` stay on the host (COM, no mouse) until each one passes a PoC inside the VM:

* **Office COM:**
  * activation without network;
  * Word, Excel and PowerPoint open, save and export;
  * the bench tasks pro-docs and pro-data.
* **AutoCAD:**
  * licence in the VM;
  * COM `autocad.draw_*` and `autocad.draw_image`;
  * performance on the Hyper-V basic display adapter (no GPU passthrough on client Hyper-V without GPU-P).
  * GPU-P (`Add-VMGpuPartitionAdapter`) is an optional later step with its own approval.
* **Paint and Notepad:** the e2e test `tests/test_vm_e2e.py`.

A tool group moves into `[vm] tools` only after its PoC passes.

## 6. Approvals needed

1. Enable the Hyper-V role (admin, reboot).
2. Which Windows license/ISO the VM uses.
3. Create the VM on D:\LGA-VM (96 GB dynamic disk, 4–12 GB RAM, 6 vCPU, no network).
4. Temporary internet for the VM during provisioning only (or an offline bundle, which is slower to prepare).
5. Whether Office/AutoCAD should be installed in the VM.

## 7. Known limits

* The browser overlay can be removed by the page itself and does not cover the Chrome frame (by design: UX).
* A tool argument that names a host file outside `runtime/vm-workspace` is refused. The planner must copy
  the file there first; there is no automatic sharing.
* vmconnect is the viewer. A viewer embedded in the chat window (live frames from `desktop.screenshot`) is
  a possible next step.
* Hyper-V socket transport, PowerShell Direct provisioning and the e2e test have not run yet: they need
  the VM.
