#!/bin/sh
# Root installer: builds Root into one file, ~/.rootcode/rootcode.pyz, and adds a `rootcode` command
# in ~/.local/bin. Nothing else is installed: Root has no dependencies beyond Python 3.9+.
set -e
REPO="https://github.com/TreyBros/root-code"
DIR="$HOME/.rootcode"
BIN="$HOME/.local/bin"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Root needs Python 3.9 or newer. Get it from https://www.python.org/downloads/" >&2
    exit 1
fi
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))'; then
    echo "Root needs Python 3.9 or newer; this machine has $(python3 --version 2>&1)." >&2
    exit 1
fi

mkdir -p "$DIR" "$BIN"
case "$0" in
    *install.sh) [ -f "$(dirname "$0")/build.py" ] && SRC="$(cd "$(dirname "$0")" && pwd)" ;;
esac
if [ -z "$SRC" ]; then                              # running from curl | sh: fetch the source
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT
    curl -fsSL "$REPO/archive/refs/heads/main.tar.gz" | tar -xz -C "$TMP"
    SRC="$TMP/root-code-main"
fi
python3 -I "$SRC/build.py" "$DIR/rootcode.pyz" >/dev/null
rm -f "$DIR/rootcode.py"                            # the single-file version from before 1.1

cat > "$BIN/rootcode" <<'LAUNCHER'
#!/bin/sh
exec python3 -I "$HOME/.rootcode/rootcode.pyz" "$@"
LAUNCHER
chmod +x "$BIN/rootcode"

echo ""
echo "  ✓ Root $(python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); import rootcode; print(rootcode.VERSION)' "$DIR/rootcode.pyz") is installed."
case ":$PATH:" in
    *":$BIN:"*) echo "  Run it from any project folder:  rootcode" ;;
    *)  echo "  Add ~/.local/bin to your PATH first, then open a new terminal:"
        echo "      echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc    # or ~/.bashrc"
        echo "  Then run it from any project folder:  rootcode" ;;
esac
echo ""
