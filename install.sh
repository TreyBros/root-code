#!/bin/sh
# Root installer: puts rootcode.py in ~/.rootcode and a `rootcode` command in ~/.local/bin.
# Nothing else is installed: Root has no dependencies beyond Python 3.9+.
set -e
SOURCE="https://raw.githubusercontent.com/TreyBros/root-code/main/rootcode.py"
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
    *install.sh) [ -f "$(dirname "$0")/rootcode.py" ] && LOCAL="$(dirname "$0")/rootcode.py" ;;
esac
if [ -n "$LOCAL" ]; then
    cp "$LOCAL" "$DIR/rootcode.py"                     # running from a clone
else
    curl -fsSL "$SOURCE" -o "$DIR/rootcode.py"          # running from curl | sh
fi

cat > "$BIN/rootcode" <<'LAUNCHER'
#!/bin/sh
exec python3 -I "$HOME/.rootcode/rootcode.py" "$@"
LAUNCHER
chmod +x "$BIN/rootcode"

echo ""
echo "  ✓ Root is installed."
case ":$PATH:" in
    *":$BIN:"*) echo "  Run it from any project folder:  rootcode" ;;
    *)  echo "  Add ~/.local/bin to your PATH first, then open a new terminal:"
        echo "      echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc    # or ~/.bashrc"
        echo "  Then run it from any project folder:  rootcode" ;;
esac
echo ""
