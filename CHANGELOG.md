# Changelog

## v0.3.0

- **AI summaries** — `ctrl-s` toggles Gemini-powered short summaries (3-6 words) in place of the full first message. Summaries are cached permanently in `~/.claude/claude-chats-summaries.json` and generated in parallel (4 workers) with progress display
- **New project folder** — `ctrl-f` in the project view prompts for a path, creates the directory, and launches Claude there
- **Smart path resolution** — project directory decoding now walks the real filesystem to correctly handle paths with hyphens, underscores, and spaces (fixes resume failures on Windows system paths like `C:\ProgramData\Packages\...`)
- **Session cwd extraction** — resume reads the real working directory from the session file instead of relying on lossy path decoding
- **`--set-key`** — CLI flag to set/update the Gemini API key (`~/.gemini_api_key`)
- Split into `.claude-chats.py` (cross-platform Python) + `claude-chats` (bash wrapper)

## v0.2.0

- **Resume chats** — press enter to resume any conversation in Claude Code
- **Skip permissions toggle** — `ctrl-p` toggles `--dangerously-skip-permissions` flag
- **New session** — `ctrl-n` starts a new Claude session from both project and chat views
- **Cross-platform** — Windows support via `.bat` wrapper

## v0.1.0

- Initial release
- Project browser with conversation counts
- Chat preview in fzf side panel
- Sortable project list (name, chat count, recent)
- Bulk delete with confirmation
- Purge empty sessions with `ctrl-d`
