#!/bin/bash
# Lightspeed uninstaller
#  - removes the module links from Klipper's extras (and their git exclude lines)
#  - removes the page links from Mainsail/Fluidd (lightspeed/ and the lightspeed.html shortcut)
#  - asks before it comments out [include lightspeed.cfg] in printer.cfg (backup kept)
#  - asks before it comments out [update_manager lightspeed] in moonraker.conf (backup kept)
# It never deletes lightspeed.cfg or this folder; it prints how to do that yourself.
# Restart Klipper afterwards when the printer is idle.
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KLIPPER="${KLIPPER_PATH:-$HOME/klipper}"
CONFIG="${CONFIG_PATH:-$HOME/printer_data/config}"
STAMP="$(date +%Y%m%d-%H%M%S)"

ask() {  # ask "question" -> 0 on y/yes; no terminal = no
    local a
    [ -t 0 ] || return 1
    read -r -p "$1 [y/N] " a
    case "$a" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

for f in lightspeed.py lightspeed_engine.py; do
    link="$KLIPPER/klippy/extras/$f"
    if [ -L "$link" ]; then
        rm -f "$link" && echo "Removed $link"
    elif [ -e "$link" ]; then
        echo "Left $link: it is a real file, not Lightspeed's link"
    fi
    EXCLUDE="$KLIPPER/.git/info/exclude"
    if [ -f "$EXCLUDE" ] && grep -qx "klippy/extras/$f" "$EXCLUDE"; then
        grep -vx "klippy/extras/$f" "$EXCLUDE" > "$EXCLUDE.tmp" && mv "$EXCLUDE.tmp" "$EXCLUDE"
    fi
done
rm -rf "$KLIPPER/klippy/extras/__pycache__/lightspeed"*.pyc 2>/dev/null

for root in "$HOME/mainsail" "$HOME/fluidd" ${WEB_ROOT:+"$WEB_ROOT"}; do
    root="${root%/}"
    [ -d "$root" ] || continue
    if [ -L "$root/lightspeed" ]; then rm -f "$root/lightspeed" && echo "Removed page link $root/lightspeed"; fi
    if [ -f "$root/lightspeed.html" ] && grep -q 'url=/lightspeed/' "$root/lightspeed.html"; then
        rm -f "$root/lightspeed.html" && echo "Removed $root/lightspeed.html"
    fi
done

PCFG="$CONFIG/printer.cfg"
if [ -f "$PCFG" ] && grep -q '^\[include lightspeed\.cfg\]' "$PCFG"; then
    if ask "Comment out [include lightspeed.cfg] in printer.cfg?"; then
        cp "$PCFG" "$PCFG.lightspeed-backup-$STAMP"
        sed -i 's/^\[include lightspeed\.cfg\]/#[include lightspeed.cfg]/' "$PCFG"
        echo "printer.cfg: include commented out (backup: printer.cfg.lightspeed-backup-$STAMP)"
    else
        echo "printer.cfg still has [include lightspeed.cfg]: remove that line before restarting Klipper."
    fi
fi

MCONF="$CONFIG/moonraker.conf"
if [ -f "$MCONF" ] && grep -q '^\[update_manager lightspeed\]' "$MCONF"; then
    if ask "Comment out [update_manager lightspeed] in moonraker.conf?"; then
        cp "$MCONF" "$MCONF.lightspeed-backup-$STAMP"
        awk 'BEGIN{s=0} /^\[/{s=($0=="[update_manager lightspeed]")} { if (s && $0 !~ /^#/ && $0 != "") print "#" $0; else print }' "$MCONF" > "$MCONF.tmp" && mv "$MCONF.tmp" "$MCONF"
        echo "moonraker.conf: [update_manager lightspeed] commented out (backup: moonraker.conf.lightspeed-backup-$STAMP)"
    else
        echo "moonraker.conf still has [update_manager lightspeed]: remove it to stop update checks."
    fi
fi

echo
echo "Done. Next:"
echo "  1. Restart Klipper when the printer is idle (FIRMWARE_RESTART, or restart the klipper service)."
echo "  2. Your settings stay in $CONFIG/lightspeed.cfg; delete it if you do not want them."
echo "  3. Delete this folder if you like:  rm -rf $REPO"
echo "  If you migrated from led_effect, your old configs are in the *.lightspeed-backup-* files next to them."
