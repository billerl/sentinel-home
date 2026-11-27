#!/bin/bash
# Downloads the offline library (Kiwix ZIM files) into the given folder: the first ~130 GB are the essentials
# (water, first aid, medicine, food, repair, Wikipedia); then more (~290 GB: Survivor Library, a US map, Army
# manuals, old farming/hunting/trapping books, prepper videos), the biggest last.
# Safe to re-run: finished files are skipped, half-finished ones resume. Each is checked against Kiwix's sha256.
DEST="${1:?usage: download.sh <folder on a big drive>}"
B=https://download.kiwix.org/zim
RESERVE_GB="${RESERVE_GB:-150}"
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
other/usda-2015_en_2025-04.zim
other/wikivet.net_en_all_maxi_2026-07a.zim
zimit/irp.fas.org_en_military-medicine_2026-10.zim
other/gardenology.org_en_all_2026-09.zim
stack_exchange/crafts.stackexchange.com_en_all_2026-10.zim
stack_exchange/ham.stackexchange.com_en_all_2026-10.zim
stack_exchange/pets.stackexchange.com_en_all_2026-10.zim
stack_exchange/woodworking.stackexchange.com_en_all_2026-10.zim
stack_exchange/earthscience.stackexchange.com_en_all_2026-10.zim
stack_exchange/mechanics.stackexchange.com_en_all_2026-10.zim
stack_exchange/biology.stackexchange.com_en_all_2026-10.zim
other/appropedia_en_all_maxi_2026-02.zim
zimit/solar.lowtechmagazine.com_mul_all_2025-01.zim
other/energypedia_en_all_maxi_2026-06.zim
libretexts/libretexts.org_en_geo_2026-01.zim
videos/canadian-prepper_en_winterprepping_2026-08.zim
videos/canadian-prepper_en_preppingfood_2026-10.zim
videos/urban-prepper_en_all_2026-08.zim
books/gutenberg_en_lcc-s_2026-11a.zim
videos/lrnselfreliance_en_all_2026-09.zim
wikibooks/wikibooks_en_all_maxi_2026-10.zim
zimit/armypubs_en_all_2024-12.zim
books/gutenberg_en_lcc-q_2026-11a.zim
maps/maps_en_united-states-of-america_2026-06.zim
zimit/survivorlibrary.com_en_all_2026-09.zim
"
cd "$DEST" || exit 1
for f in $FILES; do
  n=$(basename "$f")
  [ -f "$n.ok" ] && continue
  # Leave RESERVE_GB free on the drive: the security video backup and the photos come first (Sentinel warns on
  # Telegram weeks before the drive fills; see backup.py "Storage early warning").
  size=$(wget -q --spider -S "$B/$f" 2>&1 | awk 'tolower($1)=="content-length:" {n=$2} END {print n+0}')
  have=$(stat -c %s "$n" 2>/dev/null || echo 0)
  free=$(df -PB1 . | awk 'NR==2 {print $4}')
  if [ "$size" -gt 0 ] && [ $((free - (size - have))) -lt $((RESERVE_GB * 1000000000)) ]; then
    echo "$(date '+%F %T') NO ROOM for $n ($((size / 1000000000)) GB; keeping $RESERVE_GB GB free for video and photos)"
    continue
  fi
  pgrep -f "repair-zim.py .*$n" >/dev/null && { echo "$(date '+%F %T') skipping $n (repair-zim is fixing it)"; continue; }
  echo "$(date '+%F %T') downloading $n"
  wget -q -c --tries=20 --retry-connrefused --waitretry=30 "$B/$f" -O "$n" || { echo "FAILED $n"; continue; }
  want=$(wget -qO- "$B/$f.sha256" | awk '{print $1}')
  have=$(sha256sum "$n" | awk '{print $1}')
  if [ -n "$want" ] && [ "$want" = "$have" ]; then touch "$n.ok"; echo "$(date '+%F %T') OK $n"
  else echo "$(date '+%F %T') CHECKSUM MISMATCH $n (want $want have $have)"; fi
done
echo "$(date '+%F %T') ALL DONE"
