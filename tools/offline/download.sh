#!/bin/bash
# Downloads the offline library (Kiwix ZIM files, about 130 GB in all) into the given folder, smallest first.
# Safe to re-run: finished files are skipped, half-finished ones resume. Each is checked against Kiwix's sha256.
DEST="${1:?usage: download.sh <folder on a big drive>}"
B=https://download.kiwix.org/zim
FILES="
other/zimgit-water_en_2024-08.zim
other/zimgit-knots_en_2024-08.zim
other/zimgit-medicine_en_2024-08.zim
other/zimgit-food-preparation_en_2025-04.zim
other/zimgit-post-disaster_en_2024-05.zim
stack_exchange/outdoors.stackexchange.com_en_all_2026-10.zim
stack_exchange/cooking.stackexchange.com_en_all_2026-10.zim
stack_exchange/gardening.stackexchange.com_en_all_2026-10.zim
stack_exchange/diy.stackexchange.com_en_all_2026-10.zim
other/mdwiki_en_all_maxi_2025-11.zim
ifixit/ifixit_en_all_2025-12.zim
wikipedia/wikipedia_en_all_maxi_2026-08.zim
"
cd "$DEST" || exit 1
for f in $FILES; do
  n=$(basename "$f")
  [ -f "$n.ok" ] && continue
  echo "$(date '+%F %T') downloading $n"
  wget -q -c --tries=20 --retry-connrefused --waitretry=30 "$B/$f" -O "$n" || { echo "FAILED $n"; continue; }
  want=$(wget -qO- "$B/$f.sha256" | awk '{print $1}')
  have=$(sha256sum "$n" | awk '{print $1}')
  if [ -n "$want" ] && [ "$want" = "$have" ]; then touch "$n.ok"; echo "$(date '+%F %T') OK $n"
  else echo "$(date '+%F %T') CHECKSUM MISMATCH $n (want $want have $have)"; fi
done
echo "$(date '+%F %T') ALL DONE"
