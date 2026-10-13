# Playbooks (skills/*.md)

Short, tested procedures for common jobs (Excel data, Word editing, organising files, web research, code, file facts,
work tracking, drawing, PDFs, system housekeeping, paths in JSON, writing text, large inputs, ambiguous deletions).
When the request matches a playbook's `triggers` (accent-free words or phrases) the planner sees the best 3 under
"Playbooks for this kind of job". They carry what running real tasks taught (which tool, the usual trap, how to check)
so a smaller or weaker model plans like an experienced one.

* Add one: create `skills/<name>.md` with `---`, `name:`, `triggers: a, b, c`, `---` and a few lines of body.
* Code: `core/rust-agent/src/skills.rs` (parser, matcher, test that all shipped playbooks load).
* Other planner safety nets added with it: tolerant JSON (`planner::repair_json`: lone backslashes, raw line breaks,
  trailing commas, cut-off replies), a "take stock" notice at steps 12/24/36, a notice after 6 lookups in a row.
* Measured: Qwen3.8-27B on the 4 tasks it failed before: 3 of 4 now pass; main brain on 111 tasks: 111/111.
