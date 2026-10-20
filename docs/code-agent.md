# Code agent (opencode) and the OpenRouter brain

## Brain
`brain_order = ["api", "cloud", "local"]` (configs/config.toml). The `api` slot is OpenRouter
(`https://openrouter.ai/api/v1`, model `minimax/minimax-m3.1-flash:free`, key `LGA_OPENROUTER_API_KEY` in the
git-ignored `.env`; set it in the chat window: Settings -> models). A rate-limited or failing free model falls
through to Ollama cloud, then the local Ollama model. No llama.cpp.

## code.run
`code.run {task, workdir, model?}` (tools/code.json, lga_worker/tools/code.py) runs the opencode coding agent
headless: `bun run --cwd <opencode>/packages/cli src/index.ts run --standalone --auto --model openrouter/...`
inside `workdir`. opencode is cloned in `../opencode` (override with `LGA_OPENCODE_DIR`; `bun install` there).
It returns opencode's final answer plus the files that changed. Planner group `code` loads on words such as
"viet code", "lap trinh", "debug", "refactor".

Policy: `code.run` is `confirm` in normal/trusted mode and `allow` in full_access. opencode runs with `--auto`
(auto-approves its own permissions) and is NOT bound by the filesystem deny patterns of this agent, so keep it
confirm unless you trust the task.
Live check 2026-10-20: `agent run` -> code.run wrote and ran fib.py (exit 0).
