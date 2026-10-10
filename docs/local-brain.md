# Local brain: gemma-4-12B Q8_0 — runtime, quality gate and benchmark

The planner's main brain is `ggml-org/gemma-4-12B-it-GGUF:Q8_0`, run by llama.cpp on the RTX 5060 Ti
16 GB. This page records how it is served, how the agent keeps it honest, and how changes are measured.
Every number here was measured on this machine (October 2026); re-measure after hardware or driver changes.

## 1. Runtime

| Before (LlamaApp router, automatic fit) | Now (the agent's own server) |
|---|---|
| model loaded wherever it fitted at the moment; next to AutoCAD or a second model it spilled into system memory | `-ngl 99 --fit off`: every layer on the GPU, never a silent spill |
| context from the model card, split over 4 parallel slots | `-c 16384 -np 1`: one fixed 16K context |
| vision projector loaded (≈0.9 GB) | `--no-mmproj` (the agent's vision runs elsewhere) |
| 110 tokens/s prompt, 6 tokens/s generation with AutoCAD open | ≈1,900 tokens/s prompt, ≈27 tokens/s generation, 15.1 GB VRAM in total |

* The gateway starts the server on demand (`[gateway.local_server] launch`, port 9940) and stops nothing
  else of yours except `Tắt Trợ lý.bat`, which stops it with the rest of the assistant.
* Two Q8 copies of the model (ours and LlamaApp's router, port 9931) do not fit the GPU. The gateway unloads
  the router's copy before starting ours (`release`), and when two answers in a row are far below GPU speed
  (prompt < 300 or generation < 10 tokens/s, or timeouts) it restarts its own server with the GPU freed and,
  meanwhile, asks the other brains first. Using LlamaApp's chat while the assistant works loads the second
  copy again: expect the assistant to slow down then.
* The chat template is the one embedded in the GGUF (`--jinja`, llama.cpp's Gemma 4 handling); the agent
  does not rewrite it.
* Planner replies are decoded under a JSON schema (`response_format: json_schema`): the reply always has the
  planner's shape and every tool name comes from the list of tools of this run. Tool arguments are then
  validated against each tool's schema and the policy before anything runs. `max_tokens` caps a runaway
  reply (3,072; 8,192 with thinking).

## 2. Generation settings (chosen by benchmark)

`thinking = true`, `temperature = 1.0`, `top_p = 0.95`, `top_k = 64` (Google's Gemma 4 baseline).

| Profile (local model only) | Pass | False success | Notes |
|---|---|---|---|
| server defaults, thinking off | 39/48 (81 %) | 1 | loops: the same read repeated dozens of times |
| T 0.2, thinking off | 18/24 (75 %) | 3 | measured while the GPU was shared (slow); not repeated |
| **Gemma 4 baseline + thinking** | **24/24 (100 %)** | **0** | ~12 s per planner call (median), ~35 s p90 |
| same, as shipped (final run, all answers from the local model) | 24/24 (100 %) | 0 | 10.5 s median / 25.6 s p90 per call, 42 s median per task |

Each planner answer may take up to 140 s on the local model before the next brain is tried.

## 3. Quality gate (what the runtime enforces, not the model)

* Every tool call: schema validation, policy (modes, protected paths, forbidden commands), then execution.
* Every state change is verified: self-verifying tools read their effect back (files, AutoCAD entities,
  whole drawings); others (clicks, typing, shell) must be proven by read-only checks before completion.
* Completion is accepted only after those checks run and pass; a completion the checks contradict is
  rejected and the planner must repair. After repeated rejections the task is completed *with the unmet
  checks reported*, never silently.
* Bounded repair: an exact call that already failed for a deterministic reason is refused (the planner must
  change something); identical calls in a row are refused; 4 failed steps in a row or the step budget end
  the task as failed with the reason — the BLOCKED outcome — instead of a pretended success.
* The planner sees what it reads (file contents, command output including formatted tables) and is told
  never to state a fact it has not observed, and to do arithmetic over data with `python.exec`.
* Content read from files, web pages and the screen is data, not instructions.

## 4. Benchmark

`tests/bench/tasks.json` (26 tasks: answers, files, Excel, CSV, multi-step, system facts, company knowledge,
documents, web, memory, honesty, GUI, AutoCAD) and `tests/bench/run_bench.py`.

```
python tests/bench/run_bench.py --profile google-think --repeat 2     # local model alone
python tests/bench/run_bench.py --profile product                     # as shipped (cloud fallbacks allowed)
python tests/bench/run_bench.py --compare logs/bench/A.json logs/bench/B.json
```

The runner judges real state (files and workbooks on disk, numbers compared with the machine's real RAM,
disk space and date, the catalog's real counts, tools used); the agent's own claim never counts. A task the
agent reports as done while a check fails is a **false success** — the release gate is zero of them.
Reports: `logs/bench/<time>-<profile>.json` / `.md`.

Gate for promoting a change: all unit tests pass, the benchmark pass rate does not drop, false successes
stay at 0. The suite is still small (26 tasks, one or two runs): grow it with every failure seen in real
use (target 100–200), and repeat important tasks to measure stability.

## 5. What the benchmark found and fixed

| Symptom | Cause | Fix |
|---|---|---|
| "the CSV has 4 rows" (37), "1+2+3 = 6" (500) | `filesystem.read` showed the planner only the file size | the observation now carries the content |
| "drive C has 1.11 GB free" | PowerShell dropped formatted table output when a script ended | output goes through `Out-String` |
| Excel creation failed 4× with the same broken PowerShell | no suitable tool; the same failing call repeated | `python.exec` (openpyxl, python-docx, pandas); an already-failed call is refused |
| "0 PDF files" (432) | filter `ext = 'pdf'`; the catalog stores `.pdf` | schema hint; a zero result comes with a reminder to check filter values |
| completed while saying "the file still has the wrong total" | planner closed early | completion rule; arithmetic via `python.exec` |
| 6 tokens/s | two model copies on a 16 GB GPU | own server, release, restart on a slow streak |
| 6 tokens/s after every knowledge search | the 2 GB embedding model loaded on the GPU next to the planner | query embeddings on the CPU (`num_gpu 0`, < 1 s) |
| loops of identical reads | thinking off | thinking on (benchmark) |

## 6. Operating applications

* The window the agent works in is brought to the front before every click and keystroke aimed at it, and
  again if another window took the front meanwhile, so input never lands in a window the person is using.
  A click at screen coordinates first brings the window under that point to the front.
* Buttons of modern applications (WPF, WinUI/XAML: Calculator, the new Notepad, Settings...) are pressed
  through UI Automation Invoke, the Windows counterpart of a DOM click: the pointer does not move. Classic
  Win32 buttons keep a real click (their Invoke can block while a dialog they open is shown). Text fields are
  filled with `desktop.set_value` (UIA ValuePattern) when possible. Web pages are driven through the DOM
  (Chrome DevTools Protocol), never the mouse.
* While the agent operates applications, a thin always-on-top banner (click-through, never focused) asks the
  person to keep their hands off and says how to stop it (the Stop button of the chat window). The person's
  mouse and keyboard are never blocked.
* A local server without a vision projector receives no screenshots (`vision = false`): the request goes
  through with a note pointing to the vision.* tools instead of failing.

## 7. Not done yet

* Native tool calling through the Gemma 4 chat template instead of the JSON reply (the gateway also drives
  Gemini, which has no shared tool format here); the JSON schema already removes malformed replies.
* Selecting a subset of tools per request (the full catalog is ~10K tokens; prefix caching makes it cheap
  after the first call of a task).
* GUI and AutoCAD tasks in unattended benchmark runs (they take over the screen).
* Fine-tuning: only from verified task records, and only after the benchmark is large enough to catch
  regressions.
