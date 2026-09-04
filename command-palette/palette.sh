#!/bin/sh
set -eu

herdr_bin=${HERDR_BIN_PATH:-herdr}
plugin_id=${HERDR_PLUGIN_ID:-brettinternet.command-palette}

for dependency in jq fzf; do
  command -v "$dependency" >/dev/null 2>&1 || {
    printf '%s is required by the command-palette plugin\n' "$dependency" >&2
    exit 1
  }
done

selected=$(
  "$herdr_bin" plugin action list |
    jq -r --arg plugin_id "$plugin_id" '.result.actions[] | select(.plugin_id != $plugin_id or .action_id != "open") | [.title, .plugin_id, .action_id] | @tsv' |
    fzf --delimiter='\t' --with-nth=1 --prompt='Herdr command> '
) || exit 0

plugin_id=$(printf '%s\n' "$selected" | cut -f2)
action_id=$(printf '%s\n' "$selected" | cut -f3)
exec "$herdr_bin" plugin action invoke "$action_id" --plugin "$plugin_id"
