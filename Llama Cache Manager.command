#!/bin/bash
#
# Llama.cpp Prompt-Cache Session Manager — macOS launcher
# -----------------------------------------------------------------
# Double-click this file in Finder (or run it from Terminal) to
# start the manager. This file is only a thin, OS-specific wrapper:
# all real program logic lives in llama_cache_manager.py so the
# behavior stays identical to any future Windows .bat front end.
#
# Tip: the first time you use this, macOS may show a security
# warning because the file isn't from a registered developer.
# Right-click (or Control-click) the file and choose "Open" once
# to approve it, or run:  xattr -d com.apple.quarantine "Llama Cache Manager.command"

set -u

# Always run from the directory this script lives in, so config.json
# and the default "llama_sessions" cache folder resolve next to it,
# no matter how the file was launched (Finder double-click, Terminal, etc).
cd "$(dirname "$0")" || {
    echo "Could not locate the script directory."
    read -n 1 -s -r -p "Press any key to close this window..."
    exit 1
}

# --- Locate a Python 3 interpreter -------------------------------------
PYTHON_BIN=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        version_output="$("$candidate" -c 'import sys; print(sys.version_info[0])' 2>/dev/null)"
        if [ "$version_output" = "3" ]; then
            PYTHON_BIN="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    echo "============================================================"
    echo " Python 3 was not found on this Mac."
    echo ""
    echo " Install it with one of the following, then re-run this file:"
    echo "   - Xcode Command Line Tools:  xcode-select --install"
    echo "   - Or download it from:       https://www.python.org/downloads/"
    echo "============================================================"
    echo ""
    read -n 1 -s -r -p "Press any key to close this window..."
    exit 1
fi

# --- Run the cross-platform program ------------------------------------
"$PYTHON_BIN" "llama_cache_manager.py" "$@"
STATUS=$?

echo ""
if [ $STATUS -ne 0 ]; then
    echo "The program exited with an error (code $STATUS)."
fi
read -n 1 -s -r -p "Press any key to close this window..."
echo ""
