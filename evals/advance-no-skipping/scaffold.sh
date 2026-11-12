#!/usr/bin/env bash
# Builds a small Node repo with no agent harness (audit rung 0).
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

git init -q -b main
git add -A
git -c user.name="Eval Fixture" -c user.email="fixture@example.invalid" commit -q -m "Initial commit"
