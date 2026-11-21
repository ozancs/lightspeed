#!/bin/bash
# Lightspeed installer (v0.1)
#  - links lightspeed.py and lightspeed_engine.py into Klipper's extras
#  - hides the links from Klipper's own git status
#  - links the page folder (web/) into ~/mainsail and ~/fluidd as lightspeed/
#  - finds [led_effect] sections and offers to move them to Lightspeed
#    (tools/migrate_led_effect.py; asks first, default no)
#  - finds a klipper-led_effect install and offers to remove its Klipper link
#    and its moonraker.conf entry (asks first, default no)
#  - creates ~/printer_data/config/lightspeed.cfg from config/lightspeed.example.cfg
#    when there is none yet
# Without a "y" answer it does not edit printer.cfg or moonraker.conf, and it
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

# Ask a yes/no question; default no, and no without a terminal
ask() {
    ANSWER=""
    if [ -t 0 ]; then
        read -r -p "$1 [y/N] " ANSWER || ANSWER=""
    else
        echo "$1 [y/N] -> no (not interactive)"
    fi
    case "$ANSWER" in [yY]*) return 0 ;; *) return 1 ;; esac
}

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
        echo
        echo "Your config has [led_effect] sections. The migration would do this:"
        echo
        "$PY" "$MIGRATE" --config "$CONFIG" --dry-run || true
        echo
        if ! "$PY" "$MIGRATE" --can-apply; then
            echo "This Lightspeed version cannot run the migrated scenes yet: update it, then run:"
            echo "  python3 $MIGRATE --apply"
        elif ask "Migrate now?"; then
            if "$PY" "$MIGRATE" --config "$CONFIG" --apply; then
                CODE=0
                "$PY" "$MIGRATE" --config "$CONFIG" --detect >/dev/null 2>&1 || CODE=$?
                if [ "$CODE" = 1 ]; then LED_EFFECT_ACTIVE=0; fi
            else
                echo "Migration failed: nothing was changed."
            fi
        else
            echo "Not migrated. Do it later with:"
            echo "  python3 $MIGRATE --dry-run"
            echo "  python3 $MIGRATE --apply"
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
            if ask "Remove led_effect.py from Klipper's extras (Lightspeed answers SET_LED_EFFECT itself)?"; then
                rm -f "$LE_LINK"
                echo "  Removed $LE_LINK"
            else
                echo "  Kept. Remove it later with: rm $LE_LINK"
            fi
        fi
        ENTRY=""
        if [ -n "$PY" ]; then
            ENTRY="$("$PY" "$MIGRATE" --moonraker "$MOON" 2>/dev/null || true)"
        fi
        if [ -n "$ENTRY" ]; then
            echo "  $MOON has this led_effect update entry:"
            echo "$ENTRY" | sed 's/^/    /'
            if ask "Comment it out in moonraker.conf (backed up first)?"; then
                "$PY" "$MIGRATE" --moonraker "$MOON" --apply || echo "  Could not edit $MOON."
            else
                echo "  Remove those lines from moonraker.conf by hand, then restart Moonraker."
            fi
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
