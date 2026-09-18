#!/usr/bin/env bash
set -euo pipefail

ACTL_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$HOME/.local/bin"
chmod +x "$ACTL_ROOT/scripts/actl"
ln -sfn "$ACTL_ROOT/scripts/actl" "$HOME/.local/bin/actl"
"$HOME/.local/bin/actl" --init

echo "Installed: $HOME/.local/bin/actl"
echo "Config:    $HOME/.config/actl/config.json"
