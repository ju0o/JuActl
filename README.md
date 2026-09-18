# actl — Agent Control CLI

A single control terminal for AI coding agents already running in tmux panes.

Supported: Claude Team, Claude Pro, OpenCode, Codex CLI, Cursor Agent CLI, CommandCode, Cline CLI, Grok CLI.

## Install

```bash
cd actl
./scripts/install.sh
```

Ensure `~/.local/bin` is in PATH, then:

```bash
actl
```

## Commands

- `/switch AGENT`
- `/copy [--print]` (deliver to clipboard, or print the exact Result)
- `/result` (alias for `/copy --print`)
- `/status [AGENT]`
- `/paste` then terminate with a line containing only `::send`
- `/discover` (suggestion-only; never rewrites config)
- `/probe [AGENT]` (read-only local storage/schema probe; skips known sensitive filenames)
- `/refresh` (re-run pane discovery + reconcile now; handles agents turned off/on or relaunched in another pane/project)
- `/config`
- `/reload`
- `/quit`

Outside the interactive control loop, `actl discover` lists every live tmux pane with its process-backed Agent detection, cwd, confidence, and mapping state. `actl discover --apply` first backs up the config, then maps only unique exact/high-confidence detections using stable pane IDs (for example `%3`); stale or mismatched mappings are removed. `actl map AGENT` presents numbered live panes and validates the selected runtime before mapping it. `actl unmap AGENT` removes one mapping. `actl copy AGENT [--print]` extracts and copies (or prints) the last response without entering the REPL.

Mappings are resynced automatically: starting `actl` (and `/refresh`) reconcile pane mappings fail-closed (remove stale, map unique strong detections) with no prompt needed, and `/copy` / prompt sends auto-re-map the agent to its unique live pane when the stored mapping went stale (agent turned off/on, relaunched, or moved panes). When several live panes of the same agent are found, `actl` prompts for inline pane selection and (for OpenCode) auto-binds the session ID so no separate `actl bind opencode` step is needed. Zero live panes still fail closed with a clear message.

Aliases: `claude-team`/`ct`/`team`, `claude-pro`/`cp`/`pro`, `opencode`/`oc`, `codex`/`cx`, `cursor`/`cu`, `commandcode`/`cmd`, `cline`/`cl`, `grok`/`gr`.

`claude-team` reads only `~/.claude-team`; `claude-pro` reads only `~/.claude-pro`. Existing single `claude` config is never migrated automatically: actl prints a warning so you can inspect `/discover` and set explicit targets.

## Safe prompt transport

Normal bracketed paste is enabled in the control terminal: pasting multiple lines produces one prompt event, rather than one prompt per line. It works over SSH terminals that preserve bracketed-paste escape sequences; `/paste` remains a delimiter-based fallback. Prompts are written verbatim to a temporary UTF-8 file, loaded into a uniquely named tmux buffer, then inserted with tmux `paste-buffer -p -r` and one separate Enter. `-p` respects the target TUI's bracketed-paste request and `-r` preserves LF rather than translating lines to Enter. The temporary file and tmux buffer are cleaned up.

## `/copy` strategies

1. CommandCode: `~/.commandcode/projects/<slug>/<uuid>.jsonl` is opened read-only; the session's `cwd` header plus process ownership (open FD, or the one file written within the process lifetime) pins the active conversation, and the final assistant `text` is extracted. Never copies a mid-stream turn.
2. OpenCode: current SQLite layout at `~/.local/share/opencode/opencode*.db` is opened read-only; newest assistant message is reconstructed from `message` + `part`. OpenCode is correlated to a specific session: `opencode --session <ID>` is proven from the live process cmdline, and a TUI launched bare (`opencode` or `opencode --auto` / yolo mode) is adopted from deterministic DB evidence (the one session in the pane's cwd that was active during the process's lifetime). Adoption fails closed when zero or several sessions qualify, and is never a global "newest session" guess.
3. Cursor: `~/.cursor/chats/**/store.db` is opened with SQLite `mode=ro`; `blobs.data` is decoded and assistant JSON is extracted.
4. Codex: `~/.codex/sessions/**/rollout-*.jsonl` is parsed read-only. The live process's open rollout is decisive; when Codex has closed the file, the rollout written within the process lifetime in the pane's cwd is adopted (same fail-closed rule).
5. Claude Team/Pro, Cline, and Grok: their own known local directories are scanned read-only for session records, correlated to the live pane process, project config, and cwd. Sensitive filenames such as auth/config/credentials are explicitly excluded.
6. Fallback: read-only `tmux capture-pane`, marked as low confidence.

No auth or credential file is modified.

## Clipboard

Priority: on a local (non-SSH) session actl uses `wl-copy` → `xclip` → `xsel`; on an SSH session it always uses the terminal clipboard via OSC 52 (`\e]52;c;…\a`), targeting the SSH client's clipboard.

The config key `clipboard_backend` (default `"auto"`) overrides the transport:
- `"auto"` — SSH → OSC 52, otherwise local clipboard first.
- `"osc52"` — always emit the OSC 52 terminal escape.
- `"local"` — always require a genuine local Wayland/X11 clipboard; fail (no silent OSC 52) when none is usable.

When running under tmux, OSC 52 is wrapped in the DCS passthrough sequence (`\ePtmux;…\e\`) when `allow-passthrough` is `on`/`all`; otherwise (the common `off` + `set-clipboard external` case) tmux itself forwards a raw OSC 52 to the outer terminal.

actl cannot verify that the outer Windows terminal actually accepted the OSC 52 sequence, so it never claims a guaranteed copy in that path — it reports "sent to terminal clipboard via OSC 52". If your terminal blocks OSC 52 (Windows Terminal supports it; some SSH clients/older terminals do not), use `/copy --print` or `/result` to print the exact extracted result for manual copy.
