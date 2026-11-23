#!/usr/bin/env bash
# Run once from inside the TunaFold folder (the one containing app.py).
set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$HERE/app.py" ] || { echo "Put this file next to app.py first."; exit 1; }
chmod +x "$HERE/t-fold"
mkdir -p "$HOME/.local/bin"
ln -sf "$HERE/t-fold" "$HOME/.local/bin/t-fold"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
     echo "Added ~/.local/bin to PATH in ~/.bashrc" ;;
esac
echo "Done. Open a NEW terminal and run: t-fold"
