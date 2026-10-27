#!/usr/bin/env bash
# Fail if tracked files mention things that should never ship in a public
# plugin: home directories, private network addresses, internal hostnames,
# e-mail addresses other than GitHub noreply, or key material.
set -euo pipefail

cd "$(dirname "$0")/.."
mapfile -t files < <(git ls-files --cached --others --exclude-standard 2>/dev/null | grep -v '^scripts/private-scan.sh$' || true)
if ((${#files[@]} == 0)); then
  mapfile -t files < <(find . -type f -not -path './.git/*' -not -name private-scan.sh -not -name '*.png' | sed 's|^\./||')
fi

patterns=(
  '/home/[A-Za-z0-9_-]+'
  '/Users/[A-Za-z0-9_-]+'
  '\b(10|127)\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\b'
  '\b192\.168\.[0-9]{1,3}\.[0-9]{1,3}\b'
  '\b172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}\b'
  '\.(int|lan|local|internal|home\.arpa)\b'
  '\.babble\.'
  'BEGIN [A-Z ]*PRIVATE KEY'
  '(ghp|glpat|gho|github_pat)_[A-Za-z0-9_]{10,}'
  '[A-Za-z0-9._%+-]+@(gmail|outlook|hotmail|yahoo|icloud|proton)\.[a-z]+'
)

status=0
for file in "${files[@]}"; do
  [[ -f $file ]] || continue
  case $file in *.png | *.jpg | *.webp) continue ;; esac
  for pattern in "${patterns[@]}"; do
    if grep -nEI -- "$pattern" "$file" >/dev/null 2>&1; then
      grep -nEI -- "$pattern" "$file" | sed "s|^|$file:|" | head -5
      status=1
    fi
  done
done

# Screenshots: refuse PNG text chunks (comments, author, software paths).
for file in "${files[@]}"; do
  case $file in
    *.png)
      if grep -aqE 'tEXt|iTXt|zTXt' "$file"; then
        echo "$file: PNG has text metadata; strip it (e.g. magick in.png -strip out.png)"
        status=1
      fi
      ;;
  esac
done

if ((status)); then echo "private-info scan FAILED"; else echo "private-info scan clean"; fi
exit "$status"
