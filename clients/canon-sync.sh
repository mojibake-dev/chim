#!/bin/sh
# canon-sync.sh -- keep a TES3MP canon folder byte-identical to the server, downloading only what
# changed. Re-run any time; safe, idempotent, self-verifying (every file checked against the manifest
# SHA-256). POSIX sh + curl + jq + (sha256sum|shasum).
#
#   Install : unzip the canon, then run this script FROM that folder -> it syncs its own folder
#   Update  : ./canon-sync.sh                                        -> only changed/new files, strays removed
#
# It syncs the folder it lives in, so no path is needed. Your invite has the manifest SHA:
#   MANIFEST_SHA=<sha> ./canon-sync.sh        (override the folder with CANON=... only if you must)
set -eu
BASE="${BASE:-https://canon.gaussing.tv/049b7997d13572010d849e8b}"   # the vvardenfell canon endpoint
# no CANON given -> sync the folder THIS script lives in (self-locate; the script ships inside the
# canon dir, so its own directory IS the canon). Override with CANON=... if you run it from elsewhere.
CANON="${CANON:-$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)}"
MANIFEST_SHA="${MANIFEST_SHA:-}"
CFG="${CFG:-}"   # the ACTIVE openmw.cfg to sync the load order into (default: this OS's OpenMW config path)
SETTINGS_CFG="${SETTINGS_CFG:-}"   # the ACTIVE settings.cfg for canon gameplay settings (default: this OS's path)
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

# 2. download only missing/changed (each verified vs the manifest before it lands). Progress ticks
#    every ~10% of the scan so a full 82k-file hash pass never looks hung; each download names its file.
ok=0; dl=0; fail=0; seen=0; nextpct=10
total=$(wc -l < "$work/want.tsv" | tr -d ' ')
echo "verifying $total local files against the manifest (full hash scan)..."
while IFS="$TAB" read -r path sha; do
  [ -n "$path" ] || continue
  seen=$((seen+1))
  if [ "$total" -gt 0 ] && [ $((seen * 100 / total)) -ge "$nextpct" ]; then
    echo "  scanned ${nextpct}% ($seen/$total); $dl fetched so far"
    while [ $((seen * 100 / total)) -ge "$nextpct" ]; do nextpct=$((nextpct+10)); done
  fi
  local="$CANON/$path"
  if [ -f "$local" ] && [ "$(sha256 "$local")" = "$sha" ]; then ok=$((ok+1)); continue; fi
  mkdir -p "$(dirname "$local")"
  enc="$(printf '%s' "$path" | jq -sRr '@uri' | sed 's/%2F/\//g')"   # url-encode, keep the slashes
  echo "  down: $path"
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
# comm MUST use the same collation as the two `LC_ALL=C sort`s above; without it, comm compares under
# the friend's ambient locale, sees C-sorted input as "not in sorted order", warns AND exits non-zero
# -> `set -eu` aborts the whole sync at stray-removal on any non-C locale. Force C here too.
LC_ALL=C comm -23 "$work/have.paths" "$work/want.paths" > "$work/strays"
while IFS= read -r stray; do
  [ -n "$stray" ] || continue
  base="${stray##*/}"
  case "$IGNORE" in *" $base "*) continue ;; esac
  rm -f "$CANON/$stray"; del=$((del+1))
done < "$work/strays"

# 4. sync the LOAD ORDER into the friend's ACTIVE openmw.cfg -- the axes TES3MP enforces
#    (content / fallback-archive / groundcover). Rewrite ONLY those three axes from the manifest,
#    keep every other line (their data= dirs, fallback= weather/lighting, [General], comments)
#    verbatim. openmw.cfg stays in IGNORE for the file sync above; here we write it at its real
#    OpenMW config-dir path, a different location from the canon tree, so there is no conflict.
if [ -z "$CFG" ]; then                     # locate the ACTIVE openmw.cfg for this OS (override: CFG=)
  case "$(uname -s)" in
    Darwin)  CFG="$HOME/Library/Preferences/openmw/openmw.cfg" ;;
    Linux)   CFG="${XDG_CONFIG_HOME:-$HOME/.config}/openmw/openmw.cfg" ;;
    *)       CFG="$HOME/Documents/My Games/OpenMW/openmw.cfg" ;;   # Windows via MSYS/Cygwin/Git-Bash
  esac
fi
mkdir -p "$(dirname "$CFG")"
# cold client: no cfg yet -> seed from the canon's shipped openmw.cfg (per-OS data= lines commented;
# uncomment your pair once, per INSTALL.md), then merge the axes into it.
if [ ! -f "$CFG" ]; then
  if [ -f "$CANON/openmw.cfg" ]; then cp "$CANON/openmw.cfg" "$CFG"
  else curl -fsSL --retry 5 --retry-delay 2 -o "$CFG" "$BASE/openmw.cfg" || : ; fi
  echo "seeded a fresh openmw.cfg at $CFG"
fi
# 4a. self-wire the two data= dirs OpenMW needs: THIS canon folder ($CANON, known for certain) and
#     your vanilla Morrowind 'Data Files'. Add only what's MISSING (any data= you already have is
#     kept), so the friend never hand-edits the cfg. The base game is autodetected in the usual Steam/
#     GOG spots; if we can't find it we print the one line to add. Only ACTIVE (uncommented) data= count.
grep -E '^[[:space:]]*data[[:space:]]*=' "$CFG" 2>/dev/null \
  | sed -E 's/^[[:space:]]*data[[:space:]]*=[[:space:]]*//; s/^"//; s/"$//' > "$work/data.have" || :
[ -f "$work/data.have" ] || : > "$work/data.have"
base_present=0; canon_present=0
while IFS= read -r d; do
  [ -n "$d" ] || continue
  if [ -f "$d/Morrowind.esm" ]; then base_present=1; fi
  if [ "${d%/}" = "${CANON%/}" ]; then canon_present=1; fi   # ignore a trailing slash on either
done < "$work/data.have"
if [ -f "$CANON/Morrowind.esm" ]; then base_present=1; fi   # (the canon never ships it, but be safe)
base_dir=""
if [ "$base_present" = 0 ]; then
  for cand in \
    "$HOME/Library/Application Support/Steam/steamapps/common/Morrowind/Data Files" \
    "$HOME/.steam/steam/steamapps/common/Morrowind/Data Files" \
    "$HOME/.local/share/Steam/steamapps/common/Morrowind/Data Files" \
    "$HOME/.steam/root/steamapps/common/Morrowind/Data Files" \
    "/c/Program Files (x86)/Steam/steamapps/common/Morrowind/Data Files" \
    "/c/Program Files (x86)/GOG Galaxy/Games/Morrowind/Data Files" \
    "/c/Program Files (x86)/Bethesda Softworks/Morrowind/Data Files" \
    "$HOME/GOG Games/Morrowind/Data Files"; do
    if [ -f "$cand/Morrowind.esm" ]; then base_dir="$cand"; break; fi
  done
fi
: > "$work/data.add"
if [ -n "$base_dir" ]; then printf 'data="%s"\n' "$base_dir" >> "$work/data.add"; echo "wired Morrowind data dir -> $base_dir"; fi
if [ "$canon_present" = 0 ]; then printf 'data="%s"\n' "$CANON" >> "$work/data.add"; echo "wired canon data dir -> $CANON"; fi
if [ "$base_present" = 0 ] && [ -z "$base_dir" ]; then
  echo "canon-sync: couldn't find your Morrowind 'Data Files' automatically -- add ONE line to $CFG:" >&2
  echo "  data=\"/path/to/your/Morrowind/Data Files\"   (see INSTALL.md)" >&2
fi
cat "$work/data.add" "$CFG" > "$work/cfg.data"   # the axis pass below rewrites THIS (data= already wired)
# the three axis blocks, one value per line, in manifest order (names are quote-free -> emit bare).
jq -r '.content[].name' "$work/modlock.json" > "$work/ax.content"
jq -r '.archives[]'     "$work/modlock.json" > "$work/ax.fallback-archive"
jq -r '.groundcover[]'  "$work/modlock.json" > "$work/ax.groundcover"
# single-pass rewrite: replace each axis block at its FIRST line's position, drop later lines of
# that axis, append any axis absent originally (canonical order), keep all else verbatim.
: > "$work/cfg.new"
d_content=0; d_fa=0; d_gc=0
while IFS= read -r line || [ -n "$line" ]; do
  key=""
  case "$line" in *=*) key="$(printf '%s' "${line%%=*}" | tr -d '[:space:]' | tr 'A-Z' 'a-z')" ;; esac
  case "$key" in
    content)          [ "$d_content" = 0 ] && { sed 's/^/content=/'          "$work/ax.content"          >> "$work/cfg.new"; d_content=1; }; continue ;;
    fallback-archive) [ "$d_fa"      = 0 ] && { sed 's/^/fallback-archive=/' "$work/ax.fallback-archive" >> "$work/cfg.new"; d_fa=1;      }; continue ;;
    groundcover)      [ "$d_gc"      = 0 ] && { sed 's/^/groundcover=/'      "$work/ax.groundcover"      >> "$work/cfg.new"; d_gc=1;      }; continue ;;
  esac
  printf '%s\n' "$line" >> "$work/cfg.new"
done < "$work/cfg.data"
[ "$d_content" = 0 ] && sed 's/^/content=/'          "$work/ax.content"          >> "$work/cfg.new"
[ "$d_fa"      = 0 ] && sed 's/^/fallback-archive=/' "$work/ax.fallback-archive" >> "$work/cfg.new"
[ "$d_gc"      = 0 ] && sed 's/^/groundcover=/'      "$work/ax.groundcover"      >> "$work/cfg.new"
n_content=$(wc -l < "$work/ax.content" | tr -d ' ')
if cmp -s "$work/cfg.new" "$CFG"; then
  echo "load order already in sync ($n_content plugins)."
else
  cp "$CFG" "$CFG.$(date +%Y%m%d-%H%M%S).bak"
  mv -f "$work/cfg.new" "$CFG"
  echo "load order synced -> $CFG ($n_content content plugins; backup written)."
fi

# 5. enforce the canon's gameplay settings in the ACTIVE settings.cfg (a section-aware INI, unlike
#    openmw.cfg): keep each listed [Game] key at the canon value, add it if absent, leave every other
#    line (the friend's own graphics prefs) untouched. Currently: stop NPCs shoving the player around.
if [ -z "$SETTINGS_CFG" ]; then
  case "$(uname -s)" in
    Darwin)  SETTINGS_CFG="$HOME/Library/Preferences/openmw/settings.cfg" ;;
    Linux)   SETTINGS_CFG="${XDG_CONFIG_HOME:-$HOME/.config}/openmw/settings.cfg" ;;
    *)       SETTINGS_CFG="$HOME/Documents/My Games/OpenMW/settings.cfg" ;;
  esac
fi
if [ -f "$SETTINGS_CFG" ]; then
  awk '
    /^\[Game\]/ { ing=1; print; next }
    /^\[/       { if (ing) { if(!d1){print "NPCs avoid collisions = true";d1=1} if(!d2){print "Standing NPCs give way to moving ones = true";d2=1} } ing=0; print; next }
    { if (ing) { k=$0; sub(/[ \t]*=.*/,"",k); sub(/^[ \t]+/,"",k)
        if(k=="NPCs avoid collisions"){print "NPCs avoid collisions = true";d1=1;next}
        if(k=="Standing NPCs give way to moving ones"){print "Standing NPCs give way to moving ones = true";d2=1;next} }
      print }
    END { if (ing) { if(!d1)print "NPCs avoid collisions = true"; if(!d2)print "Standing NPCs give way to moving ones = true" }
          else if(!d1||!d2){ print "[Game]"; if(!d1)print "NPCs avoid collisions = true"; if(!d2)print "Standing NPCs give way to moving ones = true" } }
  ' "$SETTINGS_CFG" > "$work/settings.new"
  if cmp -s "$work/settings.new" "$SETTINGS_CFG"; then
    echo "gameplay settings already applied."
  else
    cp "$SETTINGS_CFG" "$SETTINGS_CFG.$(date +%Y%m%d-%H%M%S).bak"
    mv -f "$work/settings.new" "$SETTINGS_CFG"
    echo "gameplay settings applied -> $SETTINGS_CFG (NPC collision avoidance; backup written)."
  fi
else
  mkdir -p "$(dirname "$SETTINGS_CFG")"
  printf '[Game]\nNPCs avoid collisions = true\nStanding NPCs give way to moving ones = true\n' > "$SETTINGS_CFG"
  echo "created settings.cfg with canon gameplay settings -> $SETTINGS_CFG"
fi

echo "sync: $ok up-to-date, $dl downloaded, $del removed"
[ "$fail" -eq 0 ] || { echo "canon-sync: $fail file(s) FAILED" >&2; exit 1; }
echo "canon in sync."

# 6. self-update: refresh both canon-sync.* from the server so a script change propagates on its own
#    after the first manual hand-out (no more re-downloading). Done LAST, after all logic has run, so
#    replacing the running script can't corrupt this execution; the new version applies on the NEXT run.
#    Backfills a missing script too (e.g. a Windows box gains the .sh), and stays quiet when unchanged.
for s in canon-sync.sh canon-sync.ps1; do
  if curl -fsSL --retry 3 --retry-delay 2 -o "$work/$s.dl" "$BASE/$s" 2>/dev/null; then
    if [ ! -f "$CANON/$s" ] || ! cmp -s "$work/$s.dl" "$CANON/$s"; then
      cp "$work/$s.dl" "$CANON/$s"; echo "self-update: $s refreshed (re-run to use the new version)"
    fi
  fi
done
