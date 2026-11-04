#!/usr/bin/env bash
# Builds a throwaway repo, remembers some claims, changes the code under them,
# and writes a dashboard. Usage: examples/demo.sh [output.html]
set -e
HERE="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$(cd "$(dirname "${1:-$HERE/witness-report.html}")" && pwd)/$(basename "${1:-$HERE/witness-report.html}")"
D="$HERE/examples/demo-repo"; rm -rf "$D"; mkdir -p "$D/src" "$D/tests"; cd "$D"
W="python3 $HERE/witness.py"
git init -q; git config user.email demo@example.com; git config user.name demo
printf 'TOKEN_TTL_SECONDS = 3600\ndef make_token(user):\n    return sign({"u": user, "ttl": TOKEN_TTL_SECONDS})\n' > src/auth.py
printf 'POOL_SIZE = 10\ndef connect():\n    return Pool(size=POOL_SIZE)\n' > src/db.py
printf 'RETRY_LIMIT = 3\ndef charge(card, cents):\n    for _ in range(RETRY_LIMIT):\n        pass\n' > src/payments.py
printf 'CACHE_BACKEND = "redis"\n' > src/cache.py
printf 'Run with make dev. Python 3.11.\n' > README.md
printf 'def test_ok(): assert True\n' > tests/test_auth.py
git add -A; git commit -qm initial
export WITNESS_NOW=2026-11-01T09:00:00Z
$W add "Auth tokens expire after one hour" --kind fact --author agent -w grep:src/auth.py:'TOKEN_TTL_SECONDS = 3600' -w git:src/auth.py --source "auth review" >/dev/null
$W add "DB pool is capped at 10 connections; more starves the worker" --kind gotcha --author agent -w grep:src/db.py:'POOL_SIZE = 10' -w git:src/db.py >/dev/null
$W add "Payments retry at most 3 times, then fail loudly" --kind decision -w grep:src/payments.py:'RETRY_LIMIT = 3' -w file:src/payments.py >/dev/null
$W add "Sessions are cached in Redis" --kind fact --author agent -w grep:src/cache.py:'CACHE_BACKEND = "redis"' -w git:src/cache.py >/dev/null
$W add "Local dev runs with make dev on Python 3.11" --kind fact -w file:README.md >/dev/null
$W add "Never call charge() from a request handler directly" --kind constraint >/dev/null
$W add "Auth tests must pass before touching token code" --kind constraint -w cmd:"python3 -c 'import sys; sys.exit(0)'" -w git:tests/test_auth.py >/dev/null
$W check --allow-cmd >/dev/null || true
WITNESS_NOW=2026-11-02T09:00:00Z $W check --allow-cmd >/dev/null || true
echo "def refresh(t): pass" >> src/auth.py; git commit -qam "add refresh"
echo "# retries now configurable" >> src/payments.py; git commit -qam "payments tweak"
WITNESS_NOW=2026-11-03T09:00:00Z $W check --allow-cmd >/dev/null || true
sed -i.bak 's/POOL_SIZE = 10/POOL_SIZE = 25/' src/db.py && rm src/db.py.bak; git commit -qam "bigger pool"
sed -i.bak 's/"redis"/"memcached"/' src/cache.py && rm src/cache.py.bak; git commit -qam "swap cache"
echo "Python 3.12 now." > README.md; git commit -qam readme
export WITNESS_NOW=2026-11-04T09:30:00Z
$W check --allow-cmd || true
$W report -o "$OUT" --allow-cmd
