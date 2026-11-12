# .harness

This directory is the repo's agent harness. It is managed by harness-factory
and works the same for any coding agent: the agent reads `AGENTS.md`, and the
scripts here decide whether work is done.

What lives here:

- `config.json`: commands, core flow and limits the installer renders from.
- `installed.json`: every file the installer wrote, with its sha256 and rung.
- `ledger.jsonl`: append-only record of audits and installs.
- `bin/`, `hooks/`, `docs/`: gates, smoke test and evidence rules (rung 2+).
- `.state/`: local stamps and markers. Gitignored; safe to delete.

Commit everything except `.state/`. To change a managed file, edit
`config.json` and re-run the installer (`plan`, then `apply`, then
`verify`). Files you customize are reported as conflicts rather than being
overwritten. Run the harness-factory audit to see which rung the repo is on
and what blocks the next one.
