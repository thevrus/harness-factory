#!/usr/bin/env bash
# Builds a small Node repo at audit rung 1 with a ledger and a rules inbox
# holding one correction that has occurred once.
set -euo pipefail

mkdir -p src test

cat > package.json <<'EOF'
{
  "name": "orders-api",
  "version": "1.0.0",
  "private": true,
  "scripts": {
    "start": "node src/index.js",
    "test": "node --test"
  }
}
EOF

cat > package-lock.json <<'EOF'
{
  "name": "orders-api",
  "version": "1.0.0",
  "lockfileVersion": 3,
  "requires": true,
  "packages": {
    "": {
      "name": "orders-api",
      "version": "1.0.0"
    }
  }
}
EOF

cat > src/index.js <<'EOF'
'use strict';

function total(items) {
  return items.reduce((sum, item) => sum + item.price * item.qty, 0);
}

module.exports = { total };
EOF

cat > test/index.test.js <<'EOF'
'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { total } = require('../src/index.js');

test('total sums price times quantity', () => {
  assert.strictEqual(total([{ price: 2, qty: 3 }]), 6);
});
EOF

printf 'node_modules/\n' > .gitignore
printf '# orders-api\n\nSmall internal service that totals customer orders.\n' > README.md

mkdir -p .harness

cat > AGENTS.md <<'EOF'
<!-- harness:commands:start -->
## Commands

Maintained by harness-factory from `.harness/config.json`. Edit the config and re-run the installer instead of editing this table.

| Task | Command |
| --- | --- |
| Test | `npm test` |
| Run | `npm start` |
<!-- harness:commands:end -->

<!-- harness:context:start -->
## Things you cannot find in the code

- Totals are computed in integer cents upstream; never round them here.
<!-- harness:context:end -->
EOF

printf '@AGENTS.md\n' > CLAUDE.md

cat > .harness/config.json <<'EOF'
{
  "schema": 1,
  "default_branch": "main",
  "agents": ["claude"],
  "commands": {
    "setup": "",
    "build": "",
    "test": "npm test",
    "lint": "",
    "typecheck": "",
    "run": "npm start"
  },
  "core_flow": {
    "description": "",
    "smoke_command": ""
  },
  "protected_paths": [],
  "human_only_areas": [],
  "max_diff_lines": 400,
  "maintainers": [],
  "first_loop": {
    "review": "",
    "triage": ""
  },
  "max_open_agent_prs": 3,
  "max_fix_attempts": 2
}
EOF

cat > .harness/ledger.jsonl <<'EOF'
{"ts": "2026-10-20T09:00:00Z", "kind": "audit", "rung_achieved": 1, "next_rung": 2}
{"ts": "2026-10-27T09:00:00Z", "kind": "audit", "rung_achieved": 1, "next_rung": 2}
EOF

cat > .harness/rules-inbox.md <<'EOF'
# Rules inbox

Corrections waiting for evidence. A correction moves to a rule only after it has occurred more than once.

- 2026-10-24 | https://example.invalid/pull/12 | Agent wrote order dates as MM/DD/YYYY in an API response | root cause: missing instruction
EOF

git init -q -b main
git add -A
git -c user.name="Eval Fixture" -c user.email="fixture@example.invalid" commit -q -m "Initial commit"
