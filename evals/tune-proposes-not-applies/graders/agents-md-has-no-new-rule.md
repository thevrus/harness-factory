---
type: regex
target: { source: file, path: AGENTS.md }
flags: i
match: not_contains
pattern: 'date\s+format|MM/DD|YYYY|ISO\s*8601'
---
