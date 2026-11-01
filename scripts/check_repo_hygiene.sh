#!/usr/bin/env bash
# Fails if secrets or local data are tracked or staged. Runs locally and in CI.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
status=0

# Files tracked in HEAD plus anything staged.
files=$( { git ls-files --cached; git diff --cached --name-only --diff-filter=ACMR; } | sort -u )

# 1. Forbidden paths.
forbidden=$(printf '%s\n' "$files" \
  | grep -E '(^|/)\.env(\..+)?$|(^|/)(node_modules|\.venv[^/]*|venv|uploads|postgres_data|\.next)/|\.(pem|key|p12|sqlite3|dump|log)$' \
  | grep -vE '(^|/)\.env\.example$' || true)
if [ -n "$forbidden" ]; then
  echo "FAIL: forbidden files are tracked or staged:"
  printf '  %s\n' $forbidden
  status=1
fi

# 2. Secret-looking content (Google API keys, GitHub tokens, private keys).
# Match output is checked rather than exit status: xargs exits non-zero whenever
# any single file has no match.
scan() {
  printf '%s\n' "$files" | grep -v '^$' | xargs -I{} grep -IEnH -e "$1" -- "{}" 2>/dev/null || true
}

secret_regex='AIza[0-9A-Za-z_-]{35}|gh[pousr]_[A-Za-z0-9]{36,}|-----BEGIN [A-Z ]*PRIVATE KEY-----'
matches=$(scan "$secret_regex")
if [ -n "$matches" ]; then
  echo "FAIL: secret-looking content found:"
  printf '%s\n' "$matches" | cut -c1-60 | sed 's/^/  /'
  status=1
fi

# 3. The Gemini key must never be exposed through a browser-visible variable.
matches=$(scan 'NEXT_PUBLIC_[A-Z_]*(GEMINI|SECRET|API_KEY)')
if [ -n "$matches" ]; then
  echo "FAIL: a NEXT_PUBLIC_* variable appears to carry a secret:"
  printf '%s\n' "$matches" | sed 's/^/  /'
  status=1
fi

# 4. The committed template must not carry a real key.
if [ -f .env.example ] && grep -qE '^GEMINI_API_KEY=.+' .env.example; then
  echo "FAIL: .env.example has a non-empty GEMINI_API_KEY."
  status=1
fi

[ "$status" -eq 0 ] && echo "OK: no secrets or local data tracked or staged."
exit "$status"
