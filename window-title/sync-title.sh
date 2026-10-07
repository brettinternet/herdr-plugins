#!/bin/sh
set -eu

herdr_bin=${HERDR_BIN_PATH:-herdr}
snapshot="$("$herdr_bin" api snapshot)"
title="$(printf '%s\n' "$snapshot" | jq -r '
  .result.snapshot as $snapshot
  | $snapshot.panes[]
  | select(.pane_id == $snapshot.focused_pane_id)
  | .terminal_title_stripped // .terminal_title // empty
')"

if [ -n "$title" ]; then
  "$herdr_bin" terminal title set "$title" >/dev/null
else
  "$herdr_bin" terminal title clear >/dev/null
fi
