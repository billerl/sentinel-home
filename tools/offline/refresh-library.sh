#!/bin/bash
# Rebuilds the offline library's catalogue from every fully downloaded (checksum-verified) ZIM file.
# kiwix-serve watches the catalogue (--monitorLibrary) and picks up new books without a restart.
DEST="${OFFLINE_LIBRARY_DIR:?set OFFLINE_LIBRARY_DIR to the folder the books are in}"
LIB="$HOME/.local/share/offline-library/library.xml"
mkdir -p "$(dirname "$LIB")"
[ -d "$DEST" ] || exit 0
TMP=$(mktemp "$LIB.XXXX")
rm -f "$TMP"
for ok in "$DEST"/*.zim.ok; do
  [ -e "$ok" ] || continue
  "$HOME/.local/opt/kiwix/kiwix-manage" "$TMP" add "${ok%.ok}" >/dev/null 2>&1
done
[ -f "$TMP" ] || exit 0
if cmp -s "$TMP" "$LIB"; then rm -f "$TMP"; else mv "$TMP" "$LIB"; fi
