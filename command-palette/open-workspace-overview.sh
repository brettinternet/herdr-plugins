#!/bin/sh
set -eu

exec "${HERDR_BIN_PATH:-herdr}" plugin pane open \
  --plugin "${HERDR_PLUGIN_ID:-brettinternet.command-palette}" \
  --entrypoint workspace-overview \
  --focus
