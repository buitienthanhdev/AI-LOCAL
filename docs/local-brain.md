# Brain order

Owner rule (2026-10-14): **no llama.cpp**. The planner's brain order is `configs/config.toml`
`[gateway] brain_order = ["primary", "fallback", "api", "cloud", "local"]`:

1. `primary` / `fallback` - Gemini Web through the two cookie-based adapters.
2. `api` - the official Gemini API (key from the chat window or `LGA_GEMINI_API_KEY`).
3. `cloud` - Ollama cloud model (`gemma4:31b-cloud`) through the local Ollama daemon.
4. `local` - a local model served by Ollama (`gemma4:12b`), started on demand.

Any local model (planner, Company Brain, vision) must be an Ollama model. Do not add an OpenAI-compatible
local server, a `llama.exe` launch or a UI-TARS server. `chrome_gemini` stays disabled.

Latency notes for the local route: one Ollama server only (a duplicate `ollama serve` shows up as an empty
`ollama ps`); keep one `num_ctx`; query embeddings run with `num_gpu 0`; cap local runs at about 50 % CPU
and do not start them while the machine is busy. The older llama.cpp benchmark results were removed with it.
