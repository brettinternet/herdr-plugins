#!/bin/sh
set -eu

herdr_bin=${HERDR_BIN_PATH:-herdr}
snapshot="$("$herdr_bin" api snapshot)"
tab_id="$(printf '%s\n' "$snapshot" | jq -r '
  .result.snapshot as $snapshot
  | $snapshot.panes[]
  | select(.pane_id == $snapshot.focused_pane_id)
  | .tab_id
')"
title="$(printf '%s\n' "$snapshot" | jq -r '
  .result.snapshot as $snapshot
  | $snapshot.panes[]
  | select(.pane_id == $snapshot.focused_pane_id)
  | [.terminal_title_stripped, .terminal_title]
  | map(select(. != null and . != ""))
  | first // empty
')"

if [ -z "$title" ]; then
  printf '%s\n' 'Focused pane has no title' >&2
  exit 1
fi

"$herdr_bin" tab rename "$tab_id" "$title" >/dev/null
