# Security model

This is a **single-user, trusted-code Linux desktop application**. Notebook/script execution intentionally grants the selected user's filesystem and process privileges. Import preview does not execute notebook outputs. There is no container, filesystem jail, malicious-code sandbox, or protection from malware running as the same OS user.

The HTTP server binds only `127.0.0.1`, checks the exact Host, checks supplied Origin, and requires a per-server token on mutation requests. It does not use cookies. It is not designed for shared hosting, reverse proxies, port forwarding, public exposure, or multi-user authorization.

Uploaded/imported ZIP paths, member counts, uncompressed sizes, and hashes are checked. Progress denominators and API fields are validated. Hashes establish consistency, not authorship or trust. Modern offline report imports regenerate active UI code instead of executing imported HTML/JavaScript; the original ZIP remains evidence. Arbitrary downloaded artifacts and optional legacy report kits should only be opened when trusted. Large JSON metadata has explicit bounds and ordinary member verification streams data.

Process signals use recorded ownership and Linux process identities. Reused PID/PGID values do not grant ownership. Missing identity evidence blocks adoption; external GPU processes are not killed. Detached processes deliberately started by arbitrary user code cannot be treated as sandbox-contained. Keep workflows cooperative and supervise external software separately.

Only reviewed code and synthetic fixtures belong in this repository. Research data, credentials, logs, run archives, and connection files stay outside Git. `scripts/check_publication.py` checks this boundary; this complements manual review and is not proof against every possible secret.

Supported security fixes target the current `0.1.x` preview. Report vulnerabilities through GitHub private vulnerability reporting if enabled; do not paste credentials, private structures, sequences, or exploit data into public issues. If private reporting is unavailable, open a minimal public issue asking for a private contact channel without exploit details.
