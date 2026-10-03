# Architecture and evidence contracts

`core.py` owns SQLite run records, queue ordering, snapshots, events, candidates, and the supervisor lock. `worker.py` executes an explicit frozen request. `workflows.py` provides native scientific-command validation, committed receipts, stop/resume behavior and candidate checks. `server.py` projects local state through a loopback API. `feedback.py` freezes, verifies, merges and renders snapshots. `util.py` owns shared boundary checks and atomic file writes. `sr56_bridge.py` / `notebooks.py` are optional integration code; datasets and the private reporting kit remain separate.

The supervisor runs one managed heavy task at a time. SQLite transactions serialize record changes. A filesystem lock prevents competing supervisors. The lock remains held while a closing supervisor thread can still dispatch. A claimed task freezes source/config/environment identity and starts a separate attempt directory and process group.

Events carry stable attempt identity and run-monotonic sequence numbers. Committed receipts, not heartbeats or file existence, determine completed work. Resume checks exact input membership, file hashes, configuration and environment. Known living descendants block overlapping launches; a reused PGID cannot grant ownership of unrelated processes.

Safety stop requires both an explicit request and an identity-matched committed boundary marker. A same-named exception alone is an error. Normal failures block automatic dependent runs and permit independent queued runs; resource failures pause scheduling. Browser closure leaves the server/worker running; a machine restart restores records and leaves the queue paused.

Feedback snapshots bind actual frozen bytes, event prefixes, attempt IDs, selected artifacts and evidence cutoffs. Repeated imports deduplicate; out-of-order snapshots preserve history; identity or event conflicts are rejected. Active imported report code is regenerated from verified data. The original feedback ZIP preserves byte-for-byte evidence; regenerated HTML is a derived view, not an unchanged manifest member.

The frontend preserves open forms, scroll, focus, and the two active viewer DOM nodes during polling. Sidebar state uses an in-memory value with guarded storage persistence, and a final CSS rule removes the entire grid column. All displayed demo tasks and resources are explicitly synthetic.
