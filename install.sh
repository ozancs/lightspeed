#!/bin/bash
# Lightspeed installer (v0.1)
#  - links lightspeed.py and lightspeed_engine.py into Klipper's extras
#  - hides the links from Klipper's own git status
#  - links the page folder (web/) into ~/mainsail and ~/fluidd as lightspeed/
#  - creates ~/printer_data/config/lightspeed.cfg from config/lightspeed.example.cfg
#    when there is none yet
# It does not edit printer.cfg and does not restart anything: it prints what to do.
# Run it again at any time; it only changes what is missing.
set -e

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KLIPPER="${KLIPPER_PATH:-$HOME/klipper}"
CONFIG="${CONFIG_PATH:-$HOME/printer_data/config}"

if [ ! -d "$KLIPPER/klippy/extras" ]; then
    echo "Klipper not found in $KLIPPER. Run again with KLIPPER_PATH=/path/to/klipper ./install.sh"
    exit 1
fi

for f in lightspeed.py lightspeed_engine.py; do
    ln -sf "$REPO/$f" "$KLIPPER/klippy/extras/$f"
    echo "Linked $f into $KLIPPER/klippy/extras"
    EXCLUDE="$KLIPPER/.git/info/exclude"
    if [ -f "$EXCLUDE" ] && ! grep -qx "klippy/extras/$f" "$EXCLUDE"; then
        echo "klippy/extras/$f" >> "$EXCLUDE"
    fi
done

LINKED=""
for root in "$HOME/mainsail" "$HOME/fluidd" ${WEB_ROOT:+"$WEB_ROOT"}; do
    root="${root%/}"
    [ -d "$root" ] || continue
    if ln -sfn "$REPO/web" "$root/lightspeed" 2>/dev/null; then
        echo "Page: linked into $root/lightspeed/"
        LINKED="$LINKED $root"
    else
        echo "Page: could not link into $root (permissions?)"
    fi
done

if [ -d "$CONFIG" ] && [ ! -f "$CONFIG/lightspeed.cfg" ]; then
    cp "$REPO/config/lightspeed.example.cfg" "$CONFIG/lightspeed.cfg"
    echo "Created $CONFIG/lightspeed.cfg from the example"
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "Next:"
echo "  1. Edit lightspeed.cfg (or design it on the page and press Save)."
if [ -f "$CONFIG/printer.cfg" ] && grep -q "^\[include lightspeed.cfg\]" "$CONFIG/printer.cfg"; then
    echo "  2. printer.cfg already includes lightspeed.cfg."
else
    echo "  2. Add this line to printer.cfg:   [include lightspeed.cfg]"
fi
echo "  3. Restart Klipper when the printer is idle (FIRMWARE_RESTART)."
echo "  4. Check with the console command: LIGHTSPEED_STATUS"
if [ -n "$LINKED" ]; then
    echo "  Page: http://${IP:-<printer>}/lightspeed/lightspeed.html"
fi
echo "  A Mainsail/Fluidd update removes the page link: run ./install.sh again after one."
