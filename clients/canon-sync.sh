#!/bin/sh
# canon-sync.sh -- keep a TES3MP canon folder byte-identical to the server, downloading only what
# changed. Re-run any time; safe, idempotent, self-verifying (every file checked against the manifest
# SHA-256). POSIX sh + curl + jq + (sha256sum|shasum).
#
#   Cold install : CANON=empty-folder ./canon-sync.sh   -> downloads the whole canon
#   Update       : ./canon-sync.sh                       -> only changed/new files, strays removed
#
# Your invite has BASE's token and MANIFEST_SHA. Set them via env:
#   BASE=https://canon.gaussing.tv/TOKEN  CANON=~/Games/MW-Canon  MANIFEST_SHA=<sha>  ./canon-sync.sh
set -eu
BASE="${BASE:-https://canon.gaussing.tv/049b7997d13572010d849e8b}"   # the vvardenfell canon endpoint
CANON="${CANON:-$HOME/Games/MW-Canon}"
MANIFEST_SHA="${MANIFEST_SHA:-}"
# files the sync never downloads or deletes (per-friend configs + docs)
IGNORE=" openmw.cfg INSTALL.md tes3mp-client-default.cfg modlock.json requiredDataFiles.json canon-sync.ps1 canon-sync.sh "

command -v curl >/dev/null 2>&1 || { echo "canon-sync: need curl" >&2; exit 1; }
command -v jq   >/dev/null 2>&1 || { echo "canon-sync: need jq (brew install jq / apt-get install jq)" >&2; exit 1; }
if command -v sha256sum >/dev/null 2>&1; then sha256(){ sha256sum "$1" | cut -d' ' -f1; }
else                                          sha256(){ shasum -a 256 "$1" | awk '{print $1}'; }; fi

work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
TAB="$(printf '\t')"

# 1. fetch + (optionally) verify the manifest -- the one trust check a friend makes.
curl -fsSL --retry 5 --retry-delay 2 -o "$work/modlock.json" "$BASE/modlock.json" \
  || { echo "canon-sync: cannot fetch $BASE/modlock.json" >&2; exit 1; }
if [ -n "$MANIFEST_SHA" ]; then
  got="$(sha256 "$work/modlock.json")"
  [ "$got" = "$MANIFEST_SHA" ] || { echo "canon-sync: manifest SHA-256 mismatch ($got != $MANIFEST_SHA)" >&2; exit 1; }
  echo "manifest verified ($MANIFEST_SHA)"
fi
mkdir -p "$CANON"
jq -r '.files[] | [.path, .sha256] | @tsv' "$work/modlock.json" > "$work/want.tsv"
echo "manifest: $(wc -l < "$work/want.tsv" | tr -d ' ') files -> $CANON"

# 2. download only missing/changed (each verified vs the manifest before it lands).
ok=0; dl=0; fail=0
while IFS="$TAB" read -r path sha; do
  [ -n "$path" ] || continue
  local="$CANON/$path"
  if [ -f "$local" ] && [ "$(sha256 "$local")" = "$sha" ]; then ok=$((ok+1)); continue; fi
  mkdir -p "$(dirname "$local")"
  enc="$(printf '%s' "$path" | jq -sRr '@uri' | sed 's/%2F/\//g')"   # url-encode, keep the slashes
  if curl -fsSL --retry 5 --retry-delay 2 -o "$local.part" "$BASE/tree/$enc" \
     && [ "$(sha256 "$local.part")" = "$sha" ]; then
    mv -f "$local.part" "$local"; dl=$((dl+1))
  else
    rm -f "$local.part"; echo "FAIL: $path" >&2; fail=$((fail+1))
  fi
done < "$work/want.tsv"

# 3. remove strays -- local files not in the manifest (except per-friend configs/docs in IGNORE).
del=0
cut -f1 "$work/want.tsv" | LC_ALL=C sort > "$work/want.paths"
( cd "$CANON" && find . -type f ) | sed 's|^\./||' | LC_ALL=C sort > "$work/have.paths"
comm -23 "$work/have.paths" "$work/want.paths" > "$work/strays"
while IFS= read -r stray; do
  [ -n "$stray" ] || continue
  base="${stray##*/}"
  case "$IGNORE" in *" $base "*) continue ;; esac
  rm -f "$CANON/$stray"; del=$((del+1))
done < "$work/strays"

echo "sync: $ok up-to-date, $dl downloaded, $del removed"
[ "$fail" -eq 0 ] || { echo "canon-sync: $fail file(s) FAILED" >&2; exit 1; }
echo "canon in sync."
