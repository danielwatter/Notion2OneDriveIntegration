#!/bin/sh
# Install from a git checkout. No elevated host access or Supervisor protection changes needed.
set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
config_dir=${1:-/config}
source_dir="$repo_dir/custom_components/notion_backup"
components_dir="$config_dir/custom_components"
target_dir="$components_dir/notion_backup"

if [ ! -f "$config_dir/configuration.yaml" ]; then
    echo "Not a Home Assistant config directory: $config_dir" >&2
    exit 1
fi
if [ ! -f "$source_dir/manifest.json" ]; then
    echo "Integration source missing in checkout." >&2
    exit 1
fi
if [ -L "$target_dir" ]; then
    echo "Target is a symlink. Resolve the existing installation before continuing." >&2
    exit 1
fi
mkdir -p "$components_dir"
stage_dir=$(mktemp -d "$components_dir/.notion_backup_stage.XXXXXX")
trap 'rm -rf -- "$stage_dir"' EXIT HUP INT TERM
cp -R "$source_dir/." "$stage_dir/"
# Ignore bytecode copied from a development checkout.
find "$stage_dir" -type d -name __pycache__ -exec rm -rf {} +
if [ -e "$target_dir" ]; then
    previous_dir=$(mktemp -d "$config_dir/.notion_backup_previous.XXXXXX")
    mv "$target_dir" "$previous_dir/notion_backup"
    if ! mv "$stage_dir" "$target_dir"; then
        mv "$previous_dir/notion_backup" "$target_dir"
        echo "Installation failed; previous version restored." >&2
        exit 1
    fi
    echo "Previous version kept in $previous_dir/notion_backup"
else
    mv "$stage_dir" "$target_dir"
fi
trap - EXIT HUP INT TERM
echo "Installed: $target_dir"
echo "Check configuration and restart Home Assistant Core: ha core check && ha core restart"
