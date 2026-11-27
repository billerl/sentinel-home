#!/bin/bash
# Downloads the free regional guides listed in local-guides.txt (folder|file|URL) into "Local guides" on the drive.
# Safe to re-run: files already there are skipped; a part-done one resumes.
DEST="${1:?usage: get-guides.sh <the library folder>/Local guides}"
LIST="$(dirname "$0")/local-guides.txt"
grep -v '^#' "$LIST" | while IFS='|' read -r folder name url; do
  [ -z "$url" ] && continue
  mkdir -p "$DEST/$folder"; out="$DEST/$folder/$name"
  [ -f "$out.ok" ] && continue
  if wget -q -c --tries=5 --waitretry=20 --timeout=60 -U "Mozilla/5.0 (X11; Linux x86_64) offline-library" "$url" -O "$out" \
     && { [[ "$name" != *.pdf ]] || head -c 5 "$out" | grep -q '%PDF'; }; then
    touch "$out.ok"; echo "$(date '+%F %T') OK $folder/$name"
  else
    echo "$(date '+%F %T') FAILED $folder/$name ($url)"; rm -f "$out"
  fi
done
echo "$(date '+%F %T') GUIDES DONE"
