#!/bin/bash
# Lightspeed installer (v0.1)
#  - links lightspeed.py and lightspeed_engine.py into Klipper's extras
#  - hides the links from Klipper's own git status
#  - links the page folder (web/) into ~/mainsail and ~/fluidd as lightspeed/
#  - finds [led_effect] sections and moves them to Lightspeed
#    (tools/migrate_led_effect.py; automatic, with backups and roll-back; LIGHTSPEED_NO_MIGRATE=1 skips it)
#  - finds a klipper-led_effect install and removes its Klipper link
#    and its moonraker.conf entry once the migration succeeded
#  - creates ~/printer_data/config/lightspeed.cfg from config/lightspeed.example.cfg
#    when there is none yet
# Apart from that migration it does not edit printer.cfg or moonraker.conf, and it
# never restarts anything: it prints what to do.
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
# 1 = led_effect may still be in use (unknown counts as in use)
LED_EFFECT_ACTIVE=1
if [ -n "$PY" ] && [ -f "$CONFIG/printer.cfg" ]; then
    CODE=0
    "$PY" "$MIGRATE" --config "$CONFIG" --detect >/dev/null 2>&1 || CODE=$?
    if [ "$CODE" = 0 ]; then
        LED_EFFECT_ACTIVE=1
        NSEC="$(grep -rhs --include='*.cfg' -c '^\[led_effect ' "$CONFIG" | awk '{s+=$1} END {print s+0}')"
        NFILE="$(grep -rls --include='*.cfg' '^\[led_effect ' "$CONFIG" | wc -l | tr -d ' ')"
        NMAC="$(grep -rhs --include='*.cfg' -cE '^[[:space:]]*(SET_LED_EFFECT|STOP_LED_EFFECTS)' "$CONFIG" | awk '{s+=$1} END {print s+0}')"
        echo
        echo "== Found an led_effect setup =="
        echo "  $NSEC [led_effect] section(s) in $NFILE file(s), and $NMAC SET_LED_EFFECT / STOP_LED_EFFECTS line(s) in your macros."
        if [ "${LIGHTSPEED_NO_MIGRATE:-0}" = 1 ]; then
            echo "  LIGHTSPEED_NO_MIGRATE=1: leaving it alone. Migrate later with: python3 $MIGRATE --apply"
        elif ! "$PY" "$MIGRATE" --can-apply; then
            echo "  This Lightspeed version cannot run the migrated scenes yet: update it, then run:"
            echo "  python3 $MIGRATE --apply"
        else
            echo "  Moving it into Lightspeed now:"
            echo "   - every effect becomes a Lightspeed scene with the same name, so your macros keep working unchanged"
            echo "   - the [led_effect] sections are commented out, every changed file is backed up first (*.lightspeed-backup-*)"
            echo "   - if anything does not check out afterwards, all files are put back as they were"
            echo
            if "$PY" "$MIGRATE" --config "$CONFIG" --apply; then
                CODE=0
                "$PY" "$MIGRATE" --config "$CONFIG" --detect >/dev/null 2>&1 || CODE=$?
                if [ "$CODE" = 1 ]; then LED_EFFECT_ACTIVE=0; fi
                echo
                echo "  Migrated. Check the strips and scenes on the page after the Klipper restart."
                echo "  To undo: put back the *.lightspeed-backup-* files next to your configs."
            else
                echo "  Migration stopped: nothing was changed. Details: python3 $MIGRATE --dry-run"
            fi
        fi
    elif [ "$CODE" = 1 ]; then
        LED_EFFECT_ACTIVE=0
    else
        echo "Could not read $CONFIG/printer.cfg to look for led_effect. See:"
        echo "  python3 $MIGRATE --dry-run"
    fi
fi

# --- klipper-led_effect install --------------------------------------------
LE_LINK="$KLIPPER/klippy/extras/led_effect.py"
LE_DIR="${LED_EFFECT_PATH:-$HOME/klipper-led_effect}"
MOON="${MOONRAKER_CONF:-$CONFIG/moonraker.conf}"
if [ -e "$LE_LINK" ] || [ -L "$LE_LINK" ] || [ -d "$LE_DIR" ]; then
    echo
    echo "klipper-led_effect is installed:"
    if [ -e "$LE_LINK" ] || [ -L "$LE_LINK" ]; then echo "  Klipper extras file: $LE_LINK"; fi
    if [ -d "$LE_DIR" ]; then echo "  folder: $LE_DIR"; fi
    if [ "$LED_EFFECT_ACTIVE" = 1 ]; then
        echo "  Keep it for now: your config still has [led_effect] sections (or could not be checked)."
    else
        if [ -e "$LE_LINK" ] || [ -L "$LE_LINK" ]; then
            rm -f "$LE_LINK"
            echo "  Removed $LE_LINK (Lightspeed answers SET_LED_EFFECT itself now)"
        fi
        ENTRY=""
        if [ -n "$PY" ]; then
            ENTRY="$("$PY" "$MIGRATE" --moonraker "$MOON" 2>/dev/null || true)"
        fi
        if [ -n "$ENTRY" ]; then
            echo "  $MOON has this led_effect update entry:"
            echo "$ENTRY" | sed 's/^/    /'
            "$PY" "$MIGRATE" --moonraker "$MOON" --apply && echo "  Commented it out (moonraker.conf backed up first)." || echo "  Could not edit $MOON: remove those lines by hand, then restart Moonraker."
        fi
        if [ -d "$LE_DIR" ]; then
            echo "  The folder is not used any more; delete it when you like: rm -rf $LE_DIR"
        fi
    fi
fi

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
    echo "  Page: http://${IP:-<printer>}/lightspeed.html"
fi
echo "  A Mainsail/Fluidd update removes the page link: run ./install.sh again after one."
