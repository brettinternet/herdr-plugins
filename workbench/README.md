# Agent workbench

`brettinternet.workbench` gives coding agents a deterministic interface for opening visible tools and running observable commands in plugin-owned Herdr panes.

## Prerequisites

- Linux or macOS, Herdr 0.8.2+, a POSIX shell, and Python 3.10+.
- Neovim for editor commands and LazyGit for LazyGit commands.
- `jq` for the setup examples and bundled agent skill.

Executables must be available on Herdr's `PATH`. Set `WORKBENCH_NVIM` or `WORKBENCH_LAZYGIT` to an executable path to override lookup.

## Install

```sh
herdr plugin install brettinternet/herdr-plugins/workbench --yes
```

For local development:

```sh
herdr plugin link "$HOME/src/herdr-plugins/workbench"
```

Resolve the installed controller path:

```sh
workbench_root="$(
  herdr plugin list --plugin brettinternet.workbench --json |
    jq -er '.result.plugins[0].plugin_root'
)"
workbench="$workbench_root/workbench.py"
```

The controller must be invoked from a Herdr-managed pane. It prints one JSON result to stdout and a JSON error to stderr.

## Commands

Inspect the calling workspace and layout:

```sh
"$workbench" layout
```

Open a file in a managed Neovim pane. The default preserves the current focus and reuses the workspace's existing managed editor:

```sh
"$workbench" editor open src/main.py --line 42 --placement right --no-focus
"$workbench" editor status
"$workbench" editor close
# Use --force only when explicitly discarding unsaved editor changes.
"$workbench" editor close --force
```

Start a visible foreground job:

```sh
"$workbench" job start --placement down --no-focus -- task check
"$workbench" job list
"$workbench" job status job-123
"$workbench" job read job-123 --lines 120
"$workbench" job cancel job-123
"$workbench" job close job-123
```

By default, job output is mirrored to the pane and a bounded rotating log so an agent can read it after completion. The current and previous log segments are capped at 5 MiB each. Use `--interactive` for a command that must be attached directly to the pane PTY:

```sh
"$workbench" job start --interactive --placement tab --focus -- python3
```

Completed job panes remain open at a shell until explicitly closed. `job close` refuses to close running jobs unless `--force` is supplied. `editor close` refuses when Neovim reports modified buffers and returns those buffers in the structured error; `editor status` reports bounded `dirtyBuffers` details when inspection is available. `editor close --force` discards those changes only for the recorded plugin-owned editor.

Open or reuse LazyGit for the current workspace:

```sh
"$workbench" lazygit open --cwd "$PWD" --placement tab --focus
"$workbench" lazygit close
```

Focus any known plugin pane:

```sh
"$workbench" pane focus w1:p2
```

Placements are `auto`, `right`, `down`, `tab`, or `zoomed`. `auto` chooses right for a wide pane and down for a tall pane.

## State and ownership

Runtime state defaults to `${XDG_STATE_HOME:-~/.local/state}/herdr/plugins/brettinternet.workbench`, matching Herdr's injected plugin state directory. Set `WORKBENCH_STATE_DIR` to override it consistently for every invocation channel. Neovim sockets use a private per-user temporary directory to avoid Unix socket path-length limits.

The controller records every pane it creates and only closes panes found through those records using Herdr's plugin-scoped pane API. Editor and LazyGit instances are scoped by Herdr workspace. Jobs receive stable `job-*` IDs, record their owned process group for forced cleanup, and retain their exit status and captured output. Stale records are never treated as proof that an editor is clean.

## Agent skill

The bundled skill is in [`skill/SKILL.md`](skill/SKILL.md). To make it available across compatible agent harnesses, link or copy the `skill` directory into the harness's user skill directory. For Pi:

```sh
mkdir -p "$HOME/.agents/skills"
ln -s "$workbench_root/skill" "$HOME/.agents/skills/herdr-workbench"
```

Restart or reload the harness after installing the skill.
