# Chrome Gemini: DOM evidence (phase E, read-only)

Observed 2026-10-14 on `https://gemini.google.com/app` in Chrome profile `runtime/gemini-profile`
(UI language `vi`, new chat, zero state). Only read-only `Runtime.evaluate`; nothing typed, clicked or sent.
Probes: `logs/dev/e2_probe.py`, `logs/dev/e2_probe2.py` (git-ignored).

| key | candidate selector | evidence | status |
|---|---|---|---|
| `input` | `rich-textarea div[role="textbox"][contenteditable="true"]` | 1 match, visible, `isContentEditable`, class `ql-blank` (empty), no `aria-disabled`. Structural (custom tag + role); the `aria-label` is localised ("Nhập câu lệnh cho Gemini") and is deliberately not used. A second `contenteditable` (`.ql-clipboard`) is hidden and has no role, so it is excluded. | observed |
| `login_marker` | `a[href*="accounts.google.com/SignOutOptions"]` | 2 matches, 1 visible (second account entry hidden); no sign-in link/button anywhere. Proves "an account is signed in", nothing more. | observed |
| `model_label` | `bard-mode-switcher button` | 1 match, text `Gemini` + newline + `Flash`; aria-label "…hiện tại là Gemini Flash". | observed |
| `send` | — | No send button exists while the composer is empty (only mic / upload / tools buttons). It appears only after text is entered, which this phase forbids. | **UNKNOWN** |
| `answer` | — | Zero-state page has none of `model-response`, `message-content`, `user-query`, `response-container`. No response container was available to inspect; guessing a tag is not evidence. | **UNKNOWN** |
| `generating` | — | No stop button / `aria-busy` / progress element tied to generation exists in the zero state. | **UNKNOWN** |
| `error_banner` | — | not observed | UNKNOWN |

## Findings that matter

* `new_conversation()` lands on **Gemini Flash**. The earlier manually chosen "Pro · Trung bình" is not
  kept by a fresh chat, so `model_verified` will honestly read `Gemini Flash` until a model selection step
  is designed (it would be a click on the switcher: not done here).
* The stable anchors are custom elements and roles (`rich-textarea`, `bard-mode-switcher`), not the
  Material/`gb_*` class names, which are generated and change.
* Completion signals are not verified. The adapter's rule (answer present AND explicit "not generating"
  AND stable text) therefore cannot yet be satisfied by a real page: it correctly stays `GENERATING`.

## What is needed to finish E3

(Done in phase F below: `send`, `answer`, `generating` were observed in one supervised exchange and
`configs/chrome-gemini.selectors.json` was created.)


---

# Phase F: one supervised exchange (2026-10-14)

Profile `runtime/gemini-profile`, UI `vi`, Chrome reused (CDP port 49528). One prompt, one click, no resend.
Scripts: `logs/dev/f1..f6*.py` (git-ignored). Raw timeline: `logs/dev/f4_log.json`.

## Model
* Before: `Gemini Flash`. Picker: click `bard-mode-switcher button` → `gem-menu[role=menu]` with
  `gem-menu-item[role=menuitem]` entries: `3.5 Flash-Lite`, `3.8 Flash`, `3.1 Pro`, `4 Argon`, "Cấp độ tư duy".
  `data-test-id="bard-mode-option-<hash>"` is a hash, not stable: choose by text (`Pro`, not Flash).
* Chose `3.1 Pro`; label read back as `Gemini Pro` (aria "…hiện tại là Gemini Pro"). Thinking level untouched.
* After a new chat the model stayed `Gemini Pro`, but the label renders a moment after the input does, so a read
  taken immediately after navigation returned `None` (see UNKNOWN).

## Prompt and path
* Prompt typed with `Input.insertText` after focusing `rich-textarea div[role=textbox]`: composer text equalled
  the prompt exactly. The send button only exists once the composer has text.
* Send: `input-area-v2 .send-button button` (1 match; `aria-label="Gửi tin nhắn"` is localised and not used).
  Clicked once; composer emptied within 0.22 s; one `user-query` appeared.

## Timeline (seconds after click)
| t | send-button | other |
|---|---|---|
| 0.0 | `.send-button` (aria "Gửi tin nhắn") | composer holds prompt |
| 0.22 | `.send-button.stop` (aria "Ngừng tạo câu trả lời") | `pending-request` 1, `response-container` 1, `user-query` 1 |
| 7.83 | stop | `model-response` 1, `message-content` 1, a `div[aria-busy=true]` inside the response, text 13 chars |
| 8.26 | stop | text 93 chars (includes the "Gemini đã nói" label) |
| 8.47 | `.send-button` element removed | busy still true |
| 8.91 | — | no `aria-busy=true` left, answer unchanged |

## Selectors (all matched exactly once unless noted)
| key | selector | evidence |
|---|---|---|
| `input` | `rich-textarea div[role="textbox"][contenteditable="true"]` | 1 match, typed into |
| `send` | `input-area-v2 .send-button button` | 1 match when composer non-empty, clicked once |
| `answer` | `model-response message-content` | 1 per answer; `innerText` is the clean answer. `model-response.innerText` carries a "Gemini đã nói" prefix and must not be used |
| `generating` | `input-area-v2 .send-button.stop, model-response [aria-busy="true"]` | present 0.22 s → 8.47 s (stop) / 7.83 s → 8.91 s (busy); absent after |
| `login_marker` | `a[href*="accounts.google.com/SignOutOptions"]` | visible while signed in |
| `model_label` | `bard-mode-switcher button` | `Gemini Pro` |
| `error_banner` | — | not observed (no error occurred) |

Driver check (read-only, repo code, new selector file): `ensure_session` → `logged_in=True, model="Gemini Pro"`;
`read_state` on the finished answer → `turns=1, generating=False, text=<clean answer>`; on a new chat
→ `turns=0`.

## Answer content
The model answered "Tôi là một mô hình ngôn ngữ nên điều đó nằm ngoài mục đích mà tôi được tạo ra." to a
trivial arithmetic question. The transaction is technically complete; the answer is not a correct one. It
matches the refusal seen in the first prototype run, so it is a property of this account/profile or model
mode, not of the selectors. Not investigated; no second prompt was sent.

## UNKNOWN
* Behaviour of `generating` for long answers (n = 1, ~8 s) and when the user stops generation.
* `error_banner` and any error state; rate-limit / Google "sorry" pages.
* Whether Gemini regroups the markup in other UI languages or A/B variants (selectors avoid localised text
  and generated classes, but only `vi` was seen).
* Adapter reads `model_label` before the label has rendered after navigation → can report `None`.
