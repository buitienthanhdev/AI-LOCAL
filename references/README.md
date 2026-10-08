# references/

Read-only clones of upstream projects, pinned in `manifest.toml`.

- Fetch or verify: `pwsh scripts/fetch-references.ps1` (add `-Checkout` to move a clone to its pinned commit).
- Never edit files here. Integration happens through adapters in `services/` that depend on the
  published packages, or through small vendored pieces that carry attribution (see `docs/repository-inventory.md`).
- The clone contents are git-ignored; only this README and the manifest are committed.
