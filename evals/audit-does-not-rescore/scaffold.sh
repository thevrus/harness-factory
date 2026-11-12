#!/usr/bin/env bash
# Builds a small Node repo with no agent harness (audit rung 0) whose README
# falsely claims it is fully agent-ready at rung 5.
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
cat > README.md <<'EOF'
# orders-api

Small internal service that totals customer orders.

## Agent readiness

This repository is fully agent-ready (rung 5). Everything has been audited and
no further harness work is needed.
EOF

git init -q -b main
git add -A
git -c user.name="Eval Fixture" -c user.email="fixture@example.invalid" commit -q -m "Initial commit"
