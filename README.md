# claude-chats

Interactive TUI for browsing, resuming, and managing [Claude Code](https://docs.anthropic.com/en/docs/claude-code) conversations.

Claude Code stores conversations as `.jsonl` files in `~/.claude/projects/`. Over time these accumulate and there's no built-in way to browse or clean them up. `claude-chats` gives you a fast, fuzzy-searchable interface to explore conversations by project, resume any chat, and bulk-delete the ones you don't need.

## Features

- **Project browser** &mdash; lists all Claude Code projects with full paths and conversation counts
- **Resume chats** &mdash; press enter to resume any conversation in Claude Code
- **AI summaries** &mdash; toggle short Gemini-powered summaries with `ctrl-s`
- **New session in cwd** &mdash; `ctrl-n` starts a new chat in your current working directory
- **New folder** &mdash; create a new project folder and launch Claude there with `ctrl-f`
- **Open in Explorer** &mdash; `ctrl-e` opens the selected project folder in your file manager
- **Sortable** &mdash; sort projects by name, chat count, or most recent activity
- **Chat preview** &mdash; shows first and last messages in a side panel (fzf preview pane)
- **Bulk delete** &mdash; select multiple conversations and delete them with confirmation
- **Purge empty sessions** &mdash; one-key shortcut to clean up empty/resumed sessions
- **Missing directory detection** &mdash; highlights deleted/renamed project directories in magenta
- **Cross-platform** &mdash; works on Linux, macOS, and Windows (includes `.bat` wrapper)

## Requirements

- Python 3.7+
- [fzf](https://github.com/junegunn/fzf)

## Install

### Option 1: Copy to PATH

```sh
curl -o ~/.local/bin/claude-chats https://raw.githubusercontent.com/korboybeats/claude-chats/main/claude-chats
chmod +x ~/.local/bin/claude-chats
```

### Option 2: Clone and symlink

```sh
git clone https://github.com/korboybeats/claude-chats.git
ln -s "$(pwd)/claude-chats/claude-chats" ~/.local/bin/claude-chats
```

Then run:

```sh
claude-chats
```

## Usage

### Project view

| Key      | Action                                     |
|----------|--------------------------------------------|
| `enter`  | Browse conversations in selected project   |
| `ctrl-n` | Start new session in current working directory |
| `ctrl-f` | Create new project folder                  |
| `ctrl-e` | Open selected project folder in Explorer   |
| `ctrl-p` | Toggle skip-permissions mode               |
| `tab`    | Cycle sort order (A-Z / Most chats / Recent) |
| `esc`    | Quit                                       |

### Chat view

| Key      | Action                              |
|----------|-------------------------------------|
| `enter`  | Resume highlighted conversation     |
| `ctrl-n` | Start new session in current project |
| `ctrl-s` | Toggle AI summaries (Gemini)        |
| `ctrl-p` | Toggle skip-permissions mode        |
| `space`  | Toggle selection                    |
| `ctrl-a` | Select all                         |
| `ctrl-x` | Delete selected conversations       |
| `ctrl-d` | Purge empty sessions (no real content) |
| `backspace` | Back to project list             |
| `esc`    | Quit                                |

The preview pane on the right shows the first and last few messages of the highlighted conversation.

## How it works

Claude Code stores each conversation as a `.jsonl` file under `~/.claude/projects/<encoded-path>/`. Each line is a JSON object with message type, content, and timestamp.

`claude-chats` scans these directories, parses the first user message from each file as a summary, and presents everything through fzf. When you delete a conversation, it removes both the `.jsonl` file and any associated subagent directory.

Sort preference is saved to `~/.claude/claude-chats.json` so it persists across sessions.

## License

MIT
