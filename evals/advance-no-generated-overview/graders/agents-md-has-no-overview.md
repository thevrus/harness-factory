---
type: regex
target: { source: file, path: AGENTS.md }
flags: im
match: not_contains
pattern: '^#{1,6}\s*(architecture|project overview|codebase overview|overview|main modules)\b'
---
