# Continual learning (what "learning" means here)

No weights change while you chat. The assistant gets better in four controlled ways; each one is visible and reversible.

| Layer | What is learned | Where | Control |
|---|---|---|---|
| Memory | episodes, corrections, rules (with source, confidence, conflicts) | `memory/persistent.db` | Settings > Bộ nhớ của AI |
| Self-written playbooks (Voyager-style) | after >= 2 **verified** successes of the same kind of request with the **same tool chain**, `skills/auto-*.md` is written: triggers, the tool order, the model's own step notes (never argument values). At most 40. | `skills/auto-*.md`, audit event `cortex/hippocampus` | Settings > Kỹ năng (list, edit, delete); a deleted one is only written again when earned again |
| Learned hints | recurring failures that have a known recovery, kept only if the bench pass rate does not drop | `memory/optimizer/hints.md` | `python tests/bench/optimize_hints.py [--evaluate]`; delete the file to forget |
| Dataset for an optional fine-tune | verified successes only, argument names but no values | `python scripts/export_training_data.py` -> `logs/train/*.jsonl` | a person runs it and decides; nothing trains automatically |

A fine-tune (LoRA through Ollama, with rehearsal of the earlier dataset to limit forgetting) is deliberately **not** wired
into the chat: it needs the whole GPU, takes hours and can make the model worse. The exported dataset is its input.

Helper "brains" around the model (deterministic, no model call): cerebellum (repairs trivial argument mistakes),
cingulate (blocks finishing over placeholders / error values), parietal (`data.aggregate`: exact sums and counts),
hippocampus (memory and the playbooks above). Every intervention is an audit event shown in the activity panel.
