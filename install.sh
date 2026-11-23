#!/bin/bash
# Lightspeed installer (v0.1)
#  - links lightspeed.py and lightspeed_engine.py into Klipper's extras
#  - hides the links from Klipper's own git status
#  - links the page folder (web/) into ~/mainsail and ~/fluidd as lightspeed/
#  - finds [led_effect] sections and moves them to Lightspeed
#    (tools/migrate_led_effect.py; automatic, with backups and roll-back; LIGHTSPEED_NO_MIGRATE=1 skips it)
#  - leaves shared klipper-led_effect and Moonraker installations in place
#  - creates ~/printer_data/config/lightspeed.cfg from config/lightspeed.example.cfg
#    when there is none yet
# Apart from that migration it does not edit printer.cfg or moonraker.conf, and it
# never restarts anything: it prints what to do.
# Run it again at any time; it only changes what is missing.
set -e

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KLIPPER="${KLIPPER_PATH:-$HOME/klipper}"
CONFIG="${CONFIG_PATH:-$HOME/printer_data/config}"
PRINTER_CFG="${PRINTER_CFG:-printer.cfg}"

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
        # short address: http://<printer>/lightspeed.html opens the page
        if [ ! -e "$root/lightspeed.html" ] || grep -q 'url=/lightspeed/' "$root/lightspeed.html" 2>/dev/null; then
            printf '<!doctype html><meta charset="utf-8"><title>Lightspeed</title><meta http-equiv="refresh" content="0; url=/lightspeed/"><a href="/lightspeed/">Lightspeed</a>\n' > "$root/lightspeed.html"
        fi
    else
        echo "Page: could not link into $root (permissions?)"
    fi
done

# --- led_effect -> Lightspeed ---------------------------------------------
MIGRATE="$REPO/tools/migrate_led_effect.py"
PY="$(command -v python3 || true)"
# A requested skip is a successful installation; a held or failed migration
# returns the tool's status so unattended installers can distinguish them.
MIGRATION_EXIT=""
if [ -n "$PY" ] && [ -f "$CONFIG/$PRINTER_CFG" ]; then
    CODE=0
    "$PY" "$MIGRATE" --config "$CONFIG" --printer-cfg "$PRINTER_CFG" --detect >/dev/null 2>&1 || CODE=$?
    if [ "$CODE" = 0 ]; then
        echo
        echo "Found led_effect in the configuration loaded by $CONFIG/$PRINTER_CFG."
        if [ "${LIGHTSPEED_NO_MIGRATE:-0}" = 1 ]; then
            MIGRATION_EXIT=0
            echo "  LIGHTSPEED_NO_MIGRATE=1: leaving it alone."
        elif ! "$PY" "$MIGRATE" --can-apply; then
            MIGRATION_EXIT=2
            echo "  This Lightspeed version cannot run the migrated scenes yet. Update it first."
        else
            echo "  Moving it into Lightspeed now:"
            echo "   - every effect becomes a Lightspeed scene with the same name, so your macros keep working unchanged"
            echo "   - the [led_effect] sections are commented out, every changed file is backed up first (*.lightspeed-backup-*)"
            echo "   - if anything does not check out afterwards, all files are put back as they were"
            echo
            CODE=0
            "$PY" "$MIGRATE" --config "$CONFIG" --printer-cfg "$PRINTER_CFG" --apply || CODE=$?
            if [ "$CODE" = 0 ]; then
                echo
                echo "  Migrated. Check the strips and scenes on the page after the Klipper restart."
                echo "  To undo: put back the *.lightspeed-backup-* files next to your configs."
            elif [ "$CODE" = 3 ]; then
                MIGRATION_EXIT="$CODE"
                echo "  Manual merge needed. The active printer configuration is unchanged."
            else
                MIGRATION_EXIT="$CODE"
                echo "  Migration did not complete. Review the tool output above."
            fi
        fi
    elif [ "$CODE" != 1 ]; then
        MIGRATION_EXIT="$CODE"
        echo "Could not read $CONFIG/$PRINTER_CFG to look for led_effect."
    fi
fi

# A Klipper checkout may serve several printer instances. This installation
# only inspected one configuration; shared extras and update entries stay put.
LE_LINK="$KLIPPER/klippy/extras/led_effect.py"
if [ -e "$LE_LINK" ] || [ -L "$LE_LINK" ]; then
    echo "Keeping $LE_LINK and its Moonraker updater for any other printers that use them."
fi
if [ -n "$MIGRATION_EXIT" ]; then
    echo "Review the migration before changing the active printer configuration:"
    printf '  %q %q --config %q --printer-cfg %q --dry-run\n' "$PY" "$MIGRATE" "$CONFIG" "$PRINTER_CFG"
    exit "$MIGRATION_EXIT"
fi

if [ -d "$CONFIG" ] && [ ! -f "$CONFIG/lightspeed.cfg" ]; then
    cp "$REPO/config/lightspeed.example.cfg" "$CONFIG/lightspeed.cfg"
    echo "Created $CONFIG/lightspeed.cfg from the example"
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "Next:"
echo "  1. Edit lightspeed.cfg (or design it on the page and press Save)."
if [ -f "$CONFIG/$PRINTER_CFG" ] && grep -Fqx "[include lightspeed.cfg]" "$CONFIG/$PRINTER_CFG"; then
    echo "  2. $PRINTER_CFG already includes lightspeed.cfg."
else
    echo "  2. Add this line to $PRINTER_CFG:   [include lightspeed.cfg]"
fi
echo "  3. Restart Klipper when the printer is idle (FIRMWARE_RESTART)."
echo "  4. Check with the console command: LIGHTSPEED_STATUS"
if [ -n "$LINKED" ]; then
    echo "  Page: http://${IP:-<printer>}/lightspeed.html"
fi
echo "  A Mainsail/Fluidd update removes the page link: run ./install.sh again after one."
