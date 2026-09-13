#!/bin/bash
# ============================================================
#  Blender Popular Add-ons Installer (macOS, Linux, Windows*)
#  Installs the most useful FREE extensions from the official
#  Blender Extensions platform + enables built-in core add-ons.
#
#  Usage (macOS/Linux):  bash install_blender_addons.sh
#  Requirements: Blender 4.2+ (command works on 5.x)
#  *Windows: run from PowerShell with: bash install_blender_addons.sh
#            (Git for Windows installed) — or use the WSL version.
# ============================================================
set -u

# ---- Locate Blender -------------------------------------------------------
BLENDER=""
if command -v blender >/dev/null 2>&1; then
    BLENDER="blender"
elif [ -x /Applications/Blender.app/Contents/MacOS/Blender ]; then
    BLENDER="/Applications/Blender.app/Contents/MacOS/Blender"
elif ls /Applications/Blender*.app/Contents/MacOS/Blender >/dev/null 2>&1; then
    BLENDER="$(ls -d /Applications/Blender*.app | sort | tail -1)/Contents/MacOS/Blender"
else
    echo "ERROR: Blender not found. Install it with:  brew install --cask blender"
    exit 1
fi
echo "Using Blender: $BLENDER"
"$BLENDER" --version | head -1

# ---- Enable online access + enable built-in core add-ons ------------------
echo ""
echo "==> Enabling online access for the Extensions platform..."
"$BLENDER" -b --python-expr "
import addon_utils, bpy
bpy.context.preferences.system.use_online_access = True
for name in ('node_wrangler',):
    try:
        addon_utils.enable(name, default_set=True, persistent=True)
        print('   core add-on enabled:', name)
    except Exception as e:
        print('   core add-on FAILED:', name, e)
bpy.ops.wm.save_userpref()
print('   preferences saved')
"

# ---- Sync the official extensions repository ------------------------------
echo ""
echo "==> Syncing extensions.blender.org..."
"$BLENDER" --command extension sync

# ---- Install popular free extensions --------------------------------------
# (names verified on extensions.blender.org — Feb 2026)
EXTENSIONS=(
    extra_mesh_objects    # stairs, spirals, antennas, gears, walls...
    extra_curve_objectes  # extra curves (note: historic typo in the id)
    archimesh             # rooms, doors, windows - architecture kit
    antlandscape          # A.N.T. Landscape - procedural terrain
    sun_position          # real-world sun/moon position (time, location)
)

echo ""
echo "==> Installing extensions..."
for ext in "${EXTENSIONS[@]}"; do
    printf '   %-22s ' "$ext"
    out="$("$BLENDER" --command extension install "$ext" 2>&1 | tail -1)"
    if echo "$out" | grep -q "Installed"; then
        echo "OK"
    elif echo "$out" | grep -q "No uninstalled packages"; then
        echo "already installed"
    else
        echo "FAILED ($out)"
    fi
done

# ---- Summary ---------------------------------------------------------------
echo ""
echo "==> Installed extensions:"
"$BLENDER" --command extension list 2>/dev/null | grep "\[installed\]" || true

echo ""
echo "Done! Restart Blender to see everything."
echo "The extensions are also available to headless renders (BlenderBridge)."
