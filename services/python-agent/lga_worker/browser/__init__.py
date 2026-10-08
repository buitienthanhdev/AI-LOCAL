"""Browser subsystem: a dedicated-profile Chrome driven over CDP through browser-use primitives.

* `chrome`   - detect, launch and kill our own Chrome (never the user's default profile).
* `state`    - pure helpers: URL/host matching, page criteria, compact state serialisation.
* `provider` - `BrowserProvider`: lazy browser-use session, crash recovery, all primitives.

`browser_use` itself is imported lazily (only when the first browser tool needs a session) because
the import is heavy; `lga_worker.tools.browser` must stay cheap to import.
"""
