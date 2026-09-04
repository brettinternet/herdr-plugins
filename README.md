# Herdr plugins

Ten independent plugins for [Herdr](https://herdr.dev), a terminal workspace manager. Each plugin directory contains its own manifest and runtime files.

## Prerequisites

All plugins support Linux and macOS and require a POSIX shell. Requirements vary by plugin:

| Requirement | Plugins |
| --- | --- |
| Herdr 0.8.2+ | `pane-title`, `workbench` |
| Herdr 0.8.0+ | All others |
| Python 3.10+ | `command-palette`, `last-workspace`, `pane-collapse`, `pane-equalize`, `pane-rotate`, `pane-title`, `workbench` |
| `jq` | `command-palette`, `previous-pane-focus`, `seamless-navigation`, `window-title` |
| `fzf` | `command-palette` |
| Neovim | `workbench` editor commands |
| LazyGit | `workbench` LazyGit commands |
| tmux or tmate | Optional seamless-navigation integration |

Dependencies must be available on the `PATH` inherited by Herdr. Workbench also supports `WORKBENCH_NVIM` and `WORKBENCH_LAZYGIT` executable overrides.

## Install

Install each plugin independently with Herdr's native GitHub installer:

| Plugin | Description | Install |
| --- | --- | --- |
| `command-palette` | Search and invoke installed plugin actions | `herdr plugin install brettinternet/herdr-plugins/command-palette --yes` |
| `last-workspace` | Toggle between the current and previous workspace | `herdr plugin install brettinternet/herdr-plugins/last-workspace --yes` |
| `pane-collapse` | Collapse and restore a pane's split ratio | `herdr plugin install brettinternet/herdr-plugins/pane-collapse --yes` |
| `pane-equalize` | Equalize pane dimensions | `herdr plugin install brettinternet/herdr-plugins/pane-equalize --yes` |
| `pane-rotate` | Rotate a pair of neighboring panes | `herdr plugin install brettinternet/herdr-plugins/pane-rotate --yes` |
| `pane-title` | Show agent and terminal titles on pane borders | `herdr plugin install brettinternet/herdr-plugins/pane-title --yes` |
| `previous-pane-focus` | Focus the nearest previously focused pane after close | `herdr plugin install brettinternet/herdr-plugins/previous-pane-focus --yes` |
| `seamless-navigation` | Navigate and resize panes through Vim and tmux | `herdr plugin install brettinternet/herdr-plugins/seamless-navigation --yes` |
| `window-title` | Mirror the focused pane title to the terminal | `herdr plugin install brettinternet/herdr-plugins/window-title --yes` |
| `workbench` | Let agents open managed editors and Git UIs and run observable foreground jobs | `herdr plugin install brettinternet/herdr-plugins/workbench --yes` |

## Local development

Clone the repository, then link any plugin directory while editing it:

```sh
git clone https://github.com/brettinternet/herdr-plugins.git "$HOME/src/herdr-plugins"
herdr plugin link "$HOME/src/herdr-plugins/<plugin>"
```

Replace `<plugin>` with a directory name from the install table.

## Configuration

`seamless-navigation` reads the active tmux server's prefix and uses its standard arrow-key bindings. It falls back to `Ctrl-B` when no server is available. Set `HERDR_TMUX_PREFIX` to a Herdr key token such as `ctrl+a` to override detection.

## Tests

Run every Python unittest file and ShellCheck locally:

```sh
find . -type f -name 'test_*.py' -print0 | xargs -0 -n1 python3
find . -type f -name '*.sh' -print0 | xargs -0 shellcheck
```
