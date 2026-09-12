#!/bin/bash
# BlenderBridge launcher
# Usage: bash start_bridge.sh   (from this directory)
cd "$(dirname "$0")" || exit 1
echo "=============================================="
echo "  BlenderBridge - local Blender render server"
echo "  Keep this window open while rendering."
echo "  Templates received over the network stay"
echo "  PENDING until you approve them here ('o')."
echo "  Stop: Ctrl-C or close this window."
echo "=============================================="
python3 bridge.py
echo ""
echo "Bridge stopped."
