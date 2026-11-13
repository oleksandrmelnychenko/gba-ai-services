#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
command -v uv >/dev/null || { echo "uv is not installed"; exit 2; }
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
rc=0

cp requirements.lock "$tmp/requirements.lock"
cp requirements-dev.lock "$tmp/requirements-dev.lock"
uv pip compile pyproject.toml --python-version 3.14 --generate-hashes -q \
  --no-header -o "$tmp/requirements.lock"
uv pip compile pyproject.toml --extra dev -c "$tmp/requirements.lock" --python-version 3.14 \
  --generate-hashes -q --no-header -o "$tmp/requirements-dev.lock"
for f in requirements.lock requirements-dev.lock; do
  if diff -q <(grep -vE '^\s*#' "$f") <(grep -vE '^\s*#' "$tmp/$f") >/dev/null; then
    echo "OK    $f matches pyproject.toml"
  else
    echo "DRIFT $f is out of sync with pyproject.toml"
    rc=1
  fi
done

if [ -x .venv/bin/python ]; then
  if diff <(grep -oE '^[A-Za-z0-9_.-]+==[^ ]+' requirements-dev.lock | tr 'A-Z_' 'a-z-' | sort) \
          <(VIRTUAL_ENV="$PWD/.venv" uv pip freeze --exclude-editable -q | grep -vE '^(pip|setuptools|wheel)==' | tr 'A-Z_' 'a-z-' | sort) >"$tmp/venv.diff"; then
    echo "OK    .venv matches requirements-dev.lock"
  else
    echo "DRIFT .venv differs from requirements-dev.lock:"
    sed 's/^/      /' "$tmp/venv.diff"
    rc=1
  fi
fi

python3 - requirements.lock <<'PY' || rc=1
import json, re, sys, urllib.request
pins = re.findall(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)", open(sys.argv[1]).read(), re.M)
body = {"queries": [{"package": {"name": n, "ecosystem": "PyPI"}, "version": v} for n, v in pins]}
req = urllib.request.Request("https://api.osv.dev/v1/querybatch", json.dumps(body).encode(),
                             {"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        results = json.load(resp)["results"]
except Exception as exc:
    print(f"WARN  osv.dev query failed: {exc}")
    sys.exit(0)
bad = 0
for (name, version), result in zip(pins, results):
    ids = [v["id"] for v in result.get("vulns", [])]
    if ids:
        bad += 1
        print(f"VULN  {name}=={version}: {', '.join(ids)}")
print(f"{'OK   ' if not bad else 'FAIL '} osv.dev: {len(pins)} pinned packages, {bad} with known advisories")
sys.exit(1 if bad else 0)
PY

if [ "${1:-}" = "--outdated" ]; then
  uv pip compile pyproject.toml --python-version 3.14 -q --no-header --upgrade -o "$tmp/latest.txt"
  diff <(grep -oE '^[A-Za-z0-9_.-]+==[^ ]+' requirements.lock) <(grep -oE '^[A-Za-z0-9_.-]+==[^ ]+' "$tmp/latest.txt") \
    | grep '^[<>]' || echo "OK    nothing newer inside the declared ranges"
fi
exit $rc
