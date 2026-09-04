---
name: herdr-workbench
description: Open managed Neovim or LazyGit panes and run observable foreground jobs in Herdr. Use when the user asks an agent to show a file, arrange their Herdr workbench, run something visibly in a pane, inspect that job, or focus a managed UI.
---

# Herdr Workbench

Use the `brettinternet.workbench` plugin instead of reproducing pane creation, ownership, Neovim RPC, or job tracking with raw commands.

Before doing anything, verify the caller is inside Herdr:

```sh
test "${HERDR_ENV:-}" = 1
```

Resolve the installed plugin for this request:

```sh
workbench_root="$(
  herdr plugin list --plugin brettinternet.workbench --json |
    jq -er '.result.plugins[0] | select(.enabled == true) | .plugin_root'
)"
workbench="$workbench_root/workbench.py"
test -f "$workbench"
```

Every public command emits one JSON object. Parse returned pane and job IDs; never predict them.

## Choose the operation

Inspect before making a layout-dependent choice:

```sh
"$workbench" layout
```

Open or reveal code:

```sh
"$workbench" editor open PATH --line LINE --column COLUMN --placement auto --no-focus
```

Use `--focus` when the user asks to show, reveal, switch to, or watch the file. Otherwise preserve focus with `--no-focus`. A workspace has one managed editor; later opens reuse it through Neovim RPC.

Inspect or close the editor explicitly:

```sh
"$workbench" editor status
"$workbench" editor close
```

`editor close` refuses modified Neovim buffers. Use `editor close --force` only when the user explicitly asks to discard unsaved changes.

Start an ordinary visible job:

```sh
"$workbench" job start --cwd "$PWD" --placement auto --no-focus -- PROGRAM ARG...
```

Pass the program and arguments separately after `--`; do not turn them into a shell command. Shell syntax requires an explicit shell argv, such as `-- sh -lc 'pipeline'`, and should only be used when the user requested shell behavior.

The default mirrors output to the visible pane and captures a durable log. Use `--interactive` only for a command that requires direct PTY behavior. Prefer the dedicated editor and LazyGit operations over an interactive job for those applications.

Use the returned job ID for follow-up:

```sh
"$workbench" job status JOB_ID
"$workbench" job read JOB_ID --lines 120
"$workbench" job cancel JOB_ID
"$workbench" job close JOB_ID
```

Open or reveal LazyGit:

```sh
"$workbench" lazygit open --cwd "$PWD" --placement auto --focus
```

Focus a returned pane:

```sh
"$workbench" pane focus PANE_ID
```

## Safety and ownership

- Default to `--no-focus`; focus only when requested or necessary for direct interaction.
- Close only editor, LazyGit, or job resources returned by this plugin.
- `editor close` refuses modified Neovim buffers; use `editor close --force` only when the user explicitly asks to discard unsaved changes.
- `job cancel` sends Ctrl-C only to the recorded owned pane.
- Do not use `job close --force` unless the user explicitly asks to terminate and close running work; forced close waits for cancellation and cleans up only the recorded job process group.
- Do not use raw key injection to control Neovim or LazyGit internals.
- Starting a command does not imply permission for unrelated destructive actions.
- Report the observed JSON state: created versus reused, pane ID, job ID, status, and exit code.
- For a long job, return control after `job start`; inspect it later with `status` or `read` instead of blocking the conversation.
