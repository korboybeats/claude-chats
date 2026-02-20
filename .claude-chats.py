#!/usr/bin/env python3
"""Interactive Claude Code chat manager. Browse, resume, and delete chats."""

import json
import os
import re
import shutil
import sys
import subprocess
import tempfile
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

if sys.version_info < (3, 7):
    print("Error: Python 3.7+ required")
    sys.exit(1)

PROJECTS_DIR = Path.home() / ".claude" / "projects"
CONFIG_FILE = Path.home() / ".claude" / "claude-chats.json"
GEMINI_KEY_FILE = Path.home() / ".gemini_api_key"
SUMMARY_CACHE = Path.home() / ".claude" / "claude-chats-summaries.json"

# Dynamic home prefix for stripping paths — works for any user/OS
# Linux: /home/user → "home-user"   Windows: C:\Users\user → "C--Users-user"
HOME_PREFIX = str(Path.home()).replace(":", "-").replace("\\", "-").replace("/", "-").lstrip("-")
IS_WINDOWS = sys.platform == "win32"


def _encode_name(name):
    """Encode a single directory name the way Claude Code does (non-alphanumeric → hyphen)."""
    return re.sub(r'[^a-zA-Z0-9]', '-', name)


def _resolve_encoded_path(root, parts, idx):
    """Recursively resolve hyphen-split parts against real filesystem entries."""
    if idx >= len(parts):
        return root
    try:
        entries = [e.name for e in os.scandir(root) if e.is_dir()]
    except (PermissionError, OSError):
        return None
    # Try longest match first — folder names with hyphens/underscores/spaces are common
    for length in range(len(parts) - idx, 0, -1):
        target = "-".join(parts[idx:idx + length])
        for entry in entries:
            if _encode_name(entry) == target:
                result = _resolve_encoded_path(os.path.join(root, entry), parts, idx + length)
                if result:
                    return result
    return None


def decode_project_dir(projects_path):
    """Decode a .claude/projects/ folder name back to the real filesystem path."""
    encoded = os.path.basename(projects_path)
    # Windows: "C--Users-korbo-Docs" → "C:/Users/korbo/Docs"
    if len(encoded) >= 3 and encoded[1:3] == "--" and encoded[0].isalpha():
        project_dir = encoded[0] + ":/" + encoded[3:].replace("-", "/")
    else:
        project_dir = encoded.replace("-", "/")
    if os.path.isdir(project_dir):
        return project_dir

    # Smart resolve: walk the real filesystem to find matching directory names
    if len(encoded) >= 3 and encoded[1:3] == "--" and encoded[0].isalpha():
        drive = encoded[0] + ":" + os.sep
        remaining = encoded[3:]
    elif encoded.startswith("-"):
        drive = os.sep
        remaining = encoded[1:]
    else:
        remaining = None
    if remaining:
        parts = remaining.split("-")
        resolved = _resolve_encoded_path(drive, parts, 0)
        if resolved and os.path.isdir(resolved):
            return resolved

    # Fallback: home-prefix stripping
    if encoded.startswith(f"-{HOME_PREFIX}-"):
        suffix = encoded[len(HOME_PREFIX) + 2:]
    elif encoded.startswith(f"{HOME_PREFIX}-"):
        suffix = encoded[len(HOME_PREFIX) + 1:]
    else:
        suffix = ""
    candidate = os.path.join(str(Path.home()), suffix.replace("-", os.sep)) if suffix else str(Path.home())
    project_dir = candidate if os.path.isdir(candidate) else str(Path.home())
    return project_dir


def _read_cwd_from_session(jsonl_path):
    """Extract the real cwd from a session's jsonl file."""
    try:
        with open(jsonl_path, "r", errors="replace") as f:
            for line in f:
                if '"cwd"' in line:
                    data = json.loads(line.strip())
                    cwd = data.get("cwd")
                    if cwd:
                        return cwd
                    break
    except Exception:
        pass
    return None


def launch_claude(project_dir, cmd, map_path=None, session_file=None):
    """Write resume file + exit (or execvp). Cleans up map_path if provided."""
    if map_path:
        try:
            os.unlink(map_path)
        except OSError:
            pass
    # If we have the session file, use cwd only for new sessions (no --resume).
    # For resumes, the decoded project dir is correct — the session's cwd may
    # differ from the project folder where the session is stored.
    if session_file and "--resume" not in cmd:
        real_cwd = _read_cwd_from_session(session_file)
        if real_cwd and os.path.isdir(real_cwd):
            project_dir = real_cwd
    # On Windows, use claude.exe to avoid recursing into claude.bat
    if IS_WINDOWS:
        cmd = "claude.exe" + cmd[6:]
    resume_file = os.environ.get("_CLAUDE_CHATS_RESUME")
    if resume_file:
        with open(resume_file, "w") as rf:
            rf.write(project_dir + "\n" + cmd)
        sys.exit(0)
    else:
        os.chdir(project_dir)
        argv = cmd.split()
        os.execvp(argv[0], argv)


# ANSI
DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[36m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
MAGENTA = "\033[35m"
RESET = "\033[0m"

FZF_COLORS = ",".join([
    "fg:#c0caf5",
    "bg:#1a1b26",
    "hl:#bb9af7",
    "fg+:#c0caf5",
    "bg+:#292e42",
    "hl+:#7dcfff",
    "info:#7aa2f7",
    "prompt:#7dcfff",
    "pointer:#ff007c",
    "marker:#9ece6a",
    "spinner:#9ece6a",
    "header:#565f89",
    "border:#27a1b9",
    "gutter:#1a1b26",
])

ANSI_RE = re.compile(r'\033\[[^m]*m')


def clear_screen():
    if IS_WINDOWS:
        os.system("cls")
    else:
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()

def term_width():
    try:
        return os.get_terminal_size().columns
    except OSError:
        return 80


COMPACT = term_width() < 70


def _fzf_version():
    """Return fzf major.minor as a float, e.g. 0.29 or 0.54."""
    try:
        out = subprocess.check_output(["fzf", "--version"], encoding="utf-8", errors="replace")
        m = re.match(r'(\d+\.\d+)', out.strip())
        return float(m.group(1)) if m else 0.0
    except Exception:
        return 0.0


FZF_VER = _fzf_version()

# Sort modes for project list
SORT_MODES = ["name", "chats", "recent"]
SORT_LABELS = {"name": "A-Z", "chats": "Most chats", "recent": "Recent"}


def load_config():
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_config(cfg):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f)


def load_summaries():
    try:
        with open(SUMMARY_CACHE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_summaries(cache):
    SUMMARY_CACHE.parent.mkdir(parents=True, exist_ok=True)
    with open(SUMMARY_CACHE, "w") as f:
        json.dump(cache, f)


def _can_skip_perms():
    """Check if --dangerously-skip-permissions can be used (not root)."""
    if hasattr(os, "getuid") and os.getuid() == 0:
        return False
    return True

def load_gemini_key():
    try:
        return GEMINI_KEY_FILE.read_text().strip()
    except FileNotFoundError:
        return None


def prompt_gemini_key():
    """Interactive prompt to paste and save a Gemini API key."""
    print(f"\n  {BOLD}Paste Gemini API key:{RESET} ", end="", flush=True)
    key = input().strip()
    if not key:
        print(f"  {DIM}Cancelled.{RESET}")
        return None
    GEMINI_KEY_FILE.write_text(key + "\n")
    print(f"  {GREEN}Key saved to {GEMINI_KEY_FILE}{RESET}\n")
    return key


def generate_summary(api_key, message):
    """Generate a short summary via Gemini 2.5 Flash Lite."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={api_key}"
    payload = json.dumps({
        "contents": [{"parts": [{"text":
            f"Summarize this chat message in 3-6 words. Just the topic, no fluff:\n\n{message}"
        }]}]
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        return data["candidates"][0]["content"]["parts"][0]["text"].strip().strip('"')
    except Exception:
        return None


def generate_missing_summaries(api_key, chats, cache):
    """Generate summaries for chats not in cache. Shows progress. Returns updated cache."""
    missing = []
    for chat in chats:
        sid = os.path.splitext(os.path.basename(chat["file"]))[0]
        if sid not in cache and chat["message"] not in ("(empty session)", "(resumed session)"):
            missing.append((sid, chat["message"]))
    if not missing:
        return cache

    total = len(missing)
    done = [0]

    def _gen(item):
        sid, msg = item
        result = generate_summary(api_key, msg)
        done[0] += 1
        sys.stdout.write(f"\r  Summarizing {done[0]}/{total}...")
        sys.stdout.flush()
        return sid, result

    sys.stdout.write(f"\r  Summarizing 0/{total}...")
    sys.stdout.flush()
    with ThreadPoolExecutor(max_workers=4) as pool:
        for sid, result in pool.map(_gen, missing):
            if result:
                cache[sid] = result
    sys.stdout.write("\r" + " " * 40 + "\r")
    sys.stdout.flush()
    save_summaries(cache)
    return cache


def strip_ansi(s):
    return ANSI_RE.sub('', s)


def fzf(lines, header, multi=False, prompt=" ", preview_cmd=None, expect_keys=None, border_label=None):
    """Run fzf. Returns (key, selections)."""
    margin = "0,1" if COMPACT else "1,2"
    border = "rounded" if FZF_VER >= 0.35 else "sharp"
    info_style = "inline-right" if FZF_VER >= 0.39 else "inline"
    args = [
        "fzf",
        "--header", header,
        "--header-first",
        "--reverse",
        "--no-sort",
        "--prompt", prompt,
        "--pointer", ">",
        "--marker", "*",
        "--border", border,
        "--margin", margin,
        "--padding", "0,1",
        "--info", info_style,
        "--color", FZF_COLORS,
        "--ansi",
    ]
    if FZF_VER >= 0.35:
        args.extend(["--border-label-pos", "3"])
        if border_label:
            args.extend(["--border-label", f" {border_label} "])
    binds = ["ctrl-a:select-all"]
    if multi:
        args.append("--multi")
        binds.append("space:toggle+down")
    if expect_keys:
        args.extend(["--expect", ",".join(expect_keys)])
    args.extend(["--bind", ",".join(binds)])
    if preview_cmd and not COMPACT:
        preview_pos = "bottom:40%:wrap:border-top" if term_width() < 100 else "right:50%:wrap:border-left"
        args.extend([
            "--preview", preview_cmd,
            "--preview-window", preview_pos,
        ])
    try:
        proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            encoding="utf-8", errors="replace",
        )
        stdout, _ = proc.communicate(input="\n".join(lines))
    except FileNotFoundError:
        print("Error: fzf not found. Install with:")
        print("  Ubuntu/Debian: sudo apt install fzf")
        print("  macOS:         brew install fzf")
        sys.exit(1)
    if proc.returncode != 0:
        return "esc", None
    if expect_keys:
        out_lines = stdout.split("\n")
        pressed = out_lines[0].strip()
        selections = [l for l in out_lines[1:] if l.strip()]
        return pressed, selections or None
    selections = [l for l in stdout.strip().split("\n") if l.strip()]
    return None, selections or None


def list_projects():
    projects = []
    for entry in os.scandir(PROJECTS_DIR):
        if not entry.is_dir():
            continue
        real = decode_project_dir(entry.path)
        home = str(Path.home())
        jb_real = os.path.realpath("/var/jb") if os.path.exists("/var/jb") else None
        missing = False
        if jb_real and real.startswith(jb_real):
            suffix = real[len(jb_real):]
            name = "/var/jb" + suffix if suffix else "/var/jb"
        elif real == home:
            encoded = os.path.basename(entry.path)
            if encoded in (f"-{HOME_PREFIX}", HOME_PREFIX):
                name = "~"
                missing = False
            else:
                prefix = f"-{HOME_PREFIX}-"
                tail = encoded[len(prefix):] if encoded.startswith(prefix) else encoded
                name = "~/" + tail
                missing = True
        elif real.startswith(home + os.sep):
            name = "~/" + real[len(home) + 1:]
            missing = not os.path.isdir(real)
        else:
            name = real
            missing = not os.path.isdir(real)
        count = 0
        newest = "0"
        try:
            for f in os.scandir(entry.path):
                if f.name.endswith(".jsonl") and f.is_file():
                    count += 1
                    mtime = f.stat().st_mtime
                    if str(mtime) > newest:
                        newest = str(mtime)
        except PermissionError:
            continue
        projects.append((name, count, entry.path, float(newest) if newest != "0" else 0, missing))
    return projects


SYSTEM_TAGS = ["<local-command-", "<command-name>", "<system-reminder>"]


def _is_system_text(text):
    """Check if text is a system/command message."""
    for tag in SYSTEM_TAGS:
        if tag in text:
            return True
    return False


def _extract_user_text(content):
    """Extract display text from a user message's content field, skipping system messages."""
    if isinstance(content, str) and content.strip():
        if _is_system_text(content):
            return ""
        return content.strip()
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text", "").strip()
                if text and not _is_system_text(text):
                    return text
    return ""


def parse_one_chat(jsonl_path):
    """Parse metadata from a single chat file."""
    try:
        stat = os.stat(jsonl_path)
        size = stat.st_size
        first_user_msg = ""
        timestamp = ""
        has_assistant = False
        bytes_read = 0
        with open(jsonl_path, "r", errors="replace") as f:
            for line in f:
                bytes_read += len(line)
                if bytes_read > 200000:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not timestamp and data.get("timestamp"):
                    timestamp = data["timestamp"]
                if data.get("type") == "assistant":
                    has_assistant = True
                if data.get("type") == "user" and not first_user_msg:
                    text = _extract_user_text(data.get("message", {}).get("content", ""))
                    if text:
                        first_user_msg = text
                        break

        first_user_msg = first_user_msg.replace("\n", " ").strip()
        if len(first_user_msg) > 120:
            first_user_msg = first_user_msg[:117] + "..."
        truly_empty = False
        if not first_user_msg:
            first_user_msg = "(resumed session)" if timestamp else "(empty session)"
            truly_empty = not has_assistant

        date_str = ""
        if timestamp:
            try:
                dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                date_str = dt.strftime("%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                date_str = timestamp[:16]

        if size < 1024:
            size_str = f"{size}B"
        elif size < 1024 * 1024:
            size_str = f"{size // 1024}K"
        else:
            size_str = f"{size // (1024 * 1024)}M"

        stem = os.path.splitext(jsonl_path)[0]
        return {
            "file": jsonl_path,
            "subagent_dir": stem,
            "date": date_str,
            "size": size_str,
            "message": first_user_msg,
            "truly_empty": truly_empty,
            "timestamp": timestamp or "0",
        }
    except Exception:
        return None


def load_chats(project_dir):
    files = [
        e.path for e in os.scandir(project_dir)
        if e.name.endswith(".jsonl") and e.is_file()
    ]
    if not files:
        return []
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = pool.map(parse_one_chat, files)
    chats = [r for r in results if r is not None]
    chats.sort(key=lambda c: c["timestamp"], reverse=True)
    return chats


def fmt_project_line(name, count, max_name_len, missing=False):
    if COMPACT:
        max_name = term_width() - 16
        display_name = name[:max_name - 1] + "~" if len(name) > max_name else name
        padding = min(max_name, max_name_len) - len(display_name) + 2
    else:
        display_name = name
        padding = max_name_len - len(name) + 2
    if missing:
        return f"  {MAGENTA}{display_name}{' ' * padding}{count:>3d} chats{RESET}"
    if count == 0:
        return f"  {DIM}{display_name}{' ' * padding}  0 chats{RESET}"
    count_color = GREEN if count < 10 else YELLOW if count < 30 else RED
    return f"  {BOLD}\033[37m{display_name}{RESET}{' ' * padding}{count_color}{count:>3d}{RESET} {DIM}chats{RESET}"


def fmt_chat_line(idx, chat, idx_width, summary=None):
    date = chat["date"] or ""
    size = chat["size"]
    msg = chat["message"]
    if COMPACT:
        # Compact: no date, truncate message to fit
        max_msg = term_width() - idx_width - 12
        if msg in ("(empty session)", "(resumed session)"):
            return f" {DIM}{idx:>{idx_width}} {size:>4s} {msg}{RESET}"
        display_text = summary or msg
        if len(display_text) > max_msg:
            display_text = display_text[:max_msg - 1] + "~"
        if summary:
            display_text = f"{CYAN}{display_text}{RESET}"
        return f" {idx:>{idx_width}} {YELLOW}{size:>4s}{RESET} {display_text}"
    if msg in ("(empty session)", "(resumed session)"):
        return f"  {DIM}{idx:>{idx_width}}  {date:<16s}  {size:>4s}  {msg}{RESET}"
    display = f"{CYAN}{summary}{RESET}" if summary else msg
    return f"  {idx:>{idx_width}}  {DIM}{date:<16s}{RESET}  {YELLOW}{size:>4s}{RESET}  {display}"


def sort_projects(projects, mode):
    if mode == "name":
        return sorted(projects, key=lambda p: p[0].lower())
    elif mode == "chats":
        return sorted(projects, key=lambda p: (-p[1], p[0].lower()))
    elif mode == "recent":
        return sorted(projects, key=lambda p: -p[3])
    return projects


# ── Preview logic (embedded from claude-chat-preview) ──────────────────────

PREVIEW_FIRST_N = 3
PREVIEW_LAST_N = 4
TS_WIDTH = 12

XML_TAG_RE = re.compile(r'<[^>]+>')


def _preview_clean_text(text):
    text = XML_TAG_RE.sub('', text)
    text = ANSI_RE.sub('', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _preview_extract_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(part["text"])
        return "\n".join(parts)
    return ""


def _preview_is_system(content):
    if isinstance(content, str):
        for tag in SYSTEM_TAGS:
            if tag in content:
                return True
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                text = part.get("text", "") or part.get("content", "")
                for tag in SYSTEM_TAGS:
                    if tag in text:
                        return True
    return False


def _preview_fmt_timestamp(ts):
    if not ts:
        return " " * TS_WIDTH
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%b %d %H:%M")
    except (ValueError, TypeError):
        return " " * TS_WIDTH


def _preview_truncate(text, max_lines=5, max_chars=300):
    lines = text.split("\n")
    remaining = 0
    if len(lines) > max_lines:
        remaining = len(lines) - max_lines
        lines = lines[:max_lines]
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars]
    if remaining:
        text += f"\n{DIM}+{remaining} more lines{RESET}"
    return text


def _preview_render_message(role, text, ts):
    out = []
    time_str = _preview_fmt_timestamp(ts)
    if role == "user":
        label = f"{GREEN}{BOLD}You   {RESET}"
    else:
        label = f"{MAGENTA}{BOLD}Claude{RESET}"
    out.append(f"  {label}  {DIM}{time_str}{RESET}")
    text = _preview_truncate(text)
    for line in text.split("\n"):
        out.append(f"    {line}")
    return "\n".join(out)


def _preview_print_section(messages, sep):
    for i, (r, t, ts) in enumerate(messages):
        print(_preview_render_message(r, t, ts))
        if i < len(messages) - 1:
            print(sep)


def _preview_read_messages(filepath, seek_from=0, max_bytes=100000):
    messages = []
    with open(filepath, "r", errors="replace") as f:
        if seek_from > 0:
            f.seek(seek_from)
            f.readline()
        bytes_read = 0
        for line in f:
            bytes_read += len(line)
            if bytes_read > max_bytes:
                break
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg_type = data.get("type")
            if msg_type == "user":
                raw = data.get("message", {}).get("content", "")
                if _preview_is_system(raw):
                    continue
                content = _preview_clean_text(_preview_extract_text(raw))
                if content:
                    messages.append(("user", content, data.get("timestamp", "")))
            elif msg_type == "assistant":
                content = _preview_clean_text(_preview_extract_text(data.get("message", {}).get("content", "")))
                if content:
                    messages.append(("assistant", content, data.get("timestamp", "")))
    return messages


def preview_main(filepath):
    """Render a chat preview for fzf's preview pane."""
    if not os.path.isfile(filepath):
        print(f"  {DIM}File not found{RESET}")
        return

    file_size = os.path.getsize(filepath)
    cols = int(os.environ.get("FZF_PREVIEW_COLUMNS", 0))
    if not cols:
        try:
            cols = os.get_terminal_size().columns // 2
        except OSError:
            cols = 40
    cols = max(cols - 3, 20)
    sep = f"  {DIM}{CYAN}{'~' * cols}{RESET}"

    first_messages = _preview_read_messages(filepath, seek_from=0, max_bytes=100000)

    read_from = max(0, file_size - 200000)
    last_messages = []
    if read_from > 0:
        last_messages = _preview_read_messages(filepath, seek_from=read_from, max_bytes=200000)

    if not first_messages and not last_messages:
        print(f"\n  {DIM}No messages{RESET}")
        return

    print()

    if read_from == 0:
        msgs = first_messages
        total = len(msgs)
        if total <= PREVIEW_FIRST_N + PREVIEW_LAST_N:
            _preview_print_section(msgs, sep)
        else:
            _preview_print_section(msgs[:PREVIEW_FIRST_N], sep)
            skipped = total - PREVIEW_FIRST_N - PREVIEW_LAST_N
            print(f"\n  {DIM}{YELLOW}        ~ {skipped} skipped{RESET}\n")
            _preview_print_section(msgs[-PREVIEW_LAST_N:], sep)
    else:
        _preview_print_section(first_messages[:PREVIEW_FIRST_N], sep)
        print(f"\n  {DIM}{YELLOW}        ~  ...  {RESET}\n")
        _preview_print_section(last_messages[-PREVIEW_LAST_N:], sep)

    print()


# ── Main UI ────────────────────────────────────────────────────────────────

def print_help():
    print("claude-chats - Browse and manage Claude Code conversations")
    print()
    print("Usage: claude-chats [OPTIONS]")
    print()
    print("Options:")
    print("  --help       Show this help message")
    print("  --set-key    Set/update Gemini API key for AI summaries")
    print()
    print("Project view:")
    print("  enter    Browse conversations in selected project")
    print("  ctrl-n   Start new session in selected project")
    print("  ctrl-f   Create new project folder")
    print("  tab      Cycle sort order (A-Z / Most chats / Recent)")
    print("  esc      Quit")
    print()
    print("Chat view:")
    print("  enter    Resume highlighted conversation in Claude Code")
    print("  ctrl-n   Start new session in current project")
    print("  space    Toggle selection")
    print("  ctrl-a   Select all")
    print("  ctrl-s   Toggle AI summaries (Gemini)")
    print("  ctrl-x   Delete selected conversations")
    print("  ctrl-d   Purge empty sessions (no real content)")
    print("  backspace  Back to project list")
    print("  esc      Quit")
    print()
    print("Requirements: Python 3.7+, fzf")


def main():
    # Handle --preview (self-invoked by fzf)
    if len(sys.argv) >= 2 and sys.argv[1] == "--preview":
        if len(sys.argv) >= 3:
            preview_main(sys.argv[2])
        return

    # Handle --preview-idx N map_file (cross-platform preview lookup)
    if len(sys.argv) >= 4 and sys.argv[1] == "--preview-idx":
        try:
            idx = int(sys.argv[2])
            with open(sys.argv[3]) as f:
                lines = f.readlines()
            if 0 <= idx < len(lines):
                preview_main(lines[idx].strip())
        except (ValueError, OSError):
            pass
        return

    # Handle --set-key
    if len(sys.argv) >= 2 and sys.argv[1] == "--set-key":
        key = prompt_gemini_key()
        if key:
            print(f"  {GREEN}Gemini API key configured.{RESET}")
        return

    # Handle --help
    if len(sys.argv) >= 2 and sys.argv[1] in ("--help", "-h"):
        print_help()
        return

    # Check projects directory exists
    if not PROJECTS_DIR.exists():
        print("No Claude Code projects found.")
        print(f"Expected: {PROJECTS_DIR}")
        sys.exit(0)

    cfg = load_config()
    saved_sort = cfg.get("sort", "name")
    sort_idx = SORT_MODES.index(saved_sort) if saved_sort in SORT_MODES else 0

    while True:
        projects = list_projects()
        if not projects:
            print("No chats found.")
            return

        sort_mode = SORT_MODES[sort_idx]
        sorted_proj = sort_projects(projects, sort_mode)

        total = sum(p[1] for p in projects)
        max_name_len = max(len(p[0]) for p in projects)

        lines = []
        project_map = {}
        for name, count, path, _, missing in sorted_proj:
            lines.append(fmt_project_line(name, count, max_name_len, missing))
            project_map[name] = (path, count)

        clear_screen()
        sort_label = SORT_LABELS[sort_mode]
        skip_perms = cfg.get("skip_permissions", False)
        perms_indicator = f"{GREEN}perms{RESET}" if skip_perms else f"{DIM}perms{RESET}"
        cwd = os.getcwd()
        home = str(Path.home())
        cwd_display = "~" + cwd[len(home):] if cwd.startswith(home) else cwd
        header = (
            f"  {DIM}{total} chats, {len(projects)} projects{RESET}\n"
            f"  {DIM}enter{RESET} open  {DIM}^n{RESET} new  {DIM}^f{RESET} folder  {DIM}^e{RESET} explorer  {DIM}^p{RESET} {perms_indicator}  {DIM}tab{RESET} {CYAN}{sort_label}{RESET}  {DIM}esc{RESET} quit"
        )
        key, selected = fzf(lines, header, prompt=" Projects > ", expect_keys=["tab", "ctrl-n", "ctrl-f", "ctrl-p", "ctrl-e"], border_label=cwd_display)

        if key == "esc":
            return
        if key == "tab":
            sort_idx = (sort_idx + 1) % len(SORT_MODES)
            cfg["sort"] = SORT_MODES[sort_idx]
            save_config(cfg)
            continue
        if key == "ctrl-p":
            cfg["skip_permissions"] = not cfg.get("skip_permissions", False)
            save_config(cfg)
            continue
        if key == "ctrl-e" and selected:
            clean = strip_ansi(selected[0]).strip()
            pname = re.sub(r'\s+\d+\s+chats\s*$', '', clean).strip()
            if pname in project_map:
                ppath, _ = project_map[pname]
                project_dir = decode_project_dir(ppath)
                if IS_WINDOWS:
                    subprocess.Popen(["explorer", project_dir])
                else:
                    subprocess.Popen(["explorer.exe", subprocess.check_output(["wslpath", "-w", project_dir], text=True).strip()])
            continue
        if key == "ctrl-n":
            cmd = "claude"
            if cfg.get("skip_permissions", False):
                cmd += " --dangerously-skip-permissions"
            launch_claude(os.getcwd(), cmd)
            continue
        if key == "ctrl-f":
            clear_screen()
            print(f"\n  {BOLD}New project folder{RESET}")
            print(f"  {DIM}Enter path (~ allowed):{RESET} ", end="", flush=True)
            folder = input().strip()
            if not folder:
                continue
            folder = os.path.expanduser(folder)
            folder = os.path.abspath(folder)
            try:
                os.makedirs(folder, exist_ok=True)
            except OSError as e:
                print(f"\n  {RED}Error: {e}{RESET}")
                input(f"\n  {DIM}Press Enter...{RESET}")
                continue
            # Create project entry so it shows up even without a chat
            encoded = re.sub(r'[^a-zA-Z0-9]', '-', folder)
            project_entry = PROJECTS_DIR / encoded
            project_entry.mkdir(parents=True, exist_ok=True)
            cmd = "claude"
            if cfg.get("skip_permissions", False):
                cmd += " --dangerously-skip-permissions"
            launch_claude(folder, cmd)
            continue
        if not selected:
            return

        clean = strip_ansi(selected[0]).strip()
        project_name = re.sub(r'\s+\d+\s+chats\s*$', '', clean).strip()
        if project_name not in project_map:
            continue

        path, count = project_map[project_name]

        if count == 0:
            clear_screen()
            print()
            print(f"  {BOLD}{project_name}{RESET}  {DIM}has no conversations.{RESET}")
            print()
            answer = input(f"  {DIM}Delete empty folder? (y/N):{RESET} ").strip().lower()
            if answer == "y":
                shutil.rmtree(path, ignore_errors=True)
                print(f"\n  {GREEN}{BOLD}Deleted folder.{RESET}\n")
            else:
                print(f"\n  {DIM}Skipped.{RESET}\n")
            input(f"  {DIM}Press Enter...{RESET}")
            continue

        # Chat view — stays in this project until user goes back or quits
        while True:
            sys.stdout.write(f"  {DIM}Loading {project_name}...{RESET}")
            sys.stdout.flush()
            chats = load_chats(path)
            clear_screen()

            if not chats:
                break

            map_fd, map_path = tempfile.mkstemp(suffix=".txt")
            with os.fdopen(map_fd, "w") as mf:
                for chat in chats:
                    mf.write(chat["file"] + "\n")

            idx_width = len(str(len(chats) - 1))
            summaries_on = cfg.get("ai_summaries", False)
            summary_cache = load_summaries() if summaries_on else {}

            def build_chat_lines():
                lines = []
                for i, chat in enumerate(chats):
                    sid = os.path.splitext(os.path.basename(chat["file"]))[0]
                    s = summary_cache.get(sid) if summaries_on else None
                    lines.append(fmt_chat_line(i, chat, idx_width, summary=s))
                return lines

            chat_lines = build_chat_lines()

            empty_indices = [i for i, c in enumerate(chats) if c["truly_empty"]]
            empty_hint = f"  {DIM}ctrl-d{RESET} purge {len(empty_indices)} empty" if empty_indices else ""
            script = os.path.realpath(__file__)
            if IS_WINDOWS:
                preview = f'python "{script}" --preview-idx {{n}} "{map_path}"'
            else:
                preview = f'"{script}" --preview-idx {{n}} "{map_path}"'

            # Inner loop for ctrl-p/ctrl-s toggles (no reload needed)
            leave_project = False
            indices = None
            while True:
                skip_perms = cfg.get("skip_permissions", False)
                perms_indicator = f"{GREEN}perms{RESET}" if skip_perms else f"{DIM}perms{RESET}"
                summ_indicator = f"{GREEN}ai{RESET}" if summaries_on else f"{DIM}ai{RESET}"
                header = (
                    f"  {BOLD}{project_name}{RESET}  {DIM}{len(chats)} chats{RESET}\n"
                    f"  {DIM}ret{RESET} go {DIM}^n{RESET} new {DIM}^p{RESET} {perms_indicator} {DIM}^s{RESET} {summ_indicator} {DIM}^x{RESET} del {DIM}bs{RESET} back"
                )
                key, selected = fzf(
                    chat_lines, header, multi=True,
                    prompt=" ",
                    preview_cmd=preview, expect_keys=["bs", "ctrl-d", "ctrl-x", "ctrl-p", "ctrl-s", "ctrl-n"],
                )

                if key == "esc":
                    os.unlink(map_path)
                    return
                if key == "ctrl-p":
                    cfg["skip_permissions"] = not cfg.get("skip_permissions", False)
                    save_config(cfg)
                    continue
                if key == "ctrl-s":
                    summaries_on = not summaries_on
                    cfg["ai_summaries"] = summaries_on
                    save_config(cfg)
                    if summaries_on:
                        api_key = load_gemini_key()
                        if not api_key:
                            api_key = prompt_gemini_key()
                        if not api_key:
                            summaries_on = False
                            cfg["ai_summaries"] = False
                            save_config(cfg)
                            continue
                        summary_cache = load_summaries()
                        summary_cache = generate_missing_summaries(api_key, chats, summary_cache)
                    chat_lines = build_chat_lines()
                    continue
                if key == "ctrl-n":
                    cmd = "claude"
                    if cfg.get("skip_permissions", False):
                        cmd += " --dangerously-skip-permissions"
                    project_dir = decode_project_dir(path)
                    launch_claude(project_dir, cmd, map_path)
                if key == "bs" or (not selected and key not in ("ctrl-d", "ctrl-x")):
                    leave_project = True
                    break

                # Resume
                if key == "" and selected:
                    clean = strip_ansi(selected[0]).strip()
                    if clean:
                        idx = int(clean.split()[0])
                        session_file = chats[idx]["file"]
                        session_id = os.path.splitext(os.path.basename(session_file))[0]
                        cmd = "claude --resume " + session_id
                        if cfg.get("skip_permissions", False):
                            cmd += " --dangerously-skip-permissions"
                        project_dir = decode_project_dir(path)
                        launch_claude(project_dir, cmd, map_path, session_file=session_file)

                if key == "ctrl-d":
                    if not empty_indices:
                        continue
                    indices = empty_indices
                elif key == "ctrl-x":
                    indices = []
                    for line in selected:
                        clean = strip_ansi(line).strip()
                        if clean:
                            indices.append(int(clean.split()[0]))
                    if not indices:
                        continue
                else:
                    continue
                break

            os.unlink(map_path)
            if leave_project:
                break

            # Delete confirmation
            clear_screen()
            try:
                cols = os.get_terminal_size().columns
            except OSError:
                cols = 80

            n = len(indices)
            label = f"conversation{'s' if n != 1 else ''}"
            total_size = 0
            for idx in indices:
                try:
                    total_size += os.path.getsize(chats[idx]["file"])
                except OSError:
                    pass
            if total_size < 1024:
                size_str = f"{total_size}B"
            elif total_size < 1024 * 1024:
                size_str = f"{total_size // 1024}KB"
            else:
                size_str = f"{total_size // (1024 * 1024)}MB"

            print()
            print(f"  {RED}{'~' * (cols - 4)}{RESET}")
            print()
            print(f"  {RED}{BOLD}  Delete {n} {label}{RESET}  {DIM}({size_str}){RESET}")
            print(f"  {DIM}  from {BOLD}{project_name}{RESET}")
            print()
            print(f"  {RED}{'~' * (cols - 4)}{RESET}")
            print()

            for idx in indices:
                chat = chats[idx]
                date = chat['date'] or '              '
                size = chat['size']
                msg = chat['message'][:65]
                print(f"    {RED}x{RESET}  {DIM}{date:<16s}{RESET}  {YELLOW}{size:>4s}{RESET}  {msg}")

            print()
            print(f"  {RED}{'~' * (cols - 4)}{RESET}")
            print()
            answer = input(f"  {BOLD}Confirm delete? {RED}y{RESET}{DIM}/{RESET}{GREEN}N{RESET} ").strip().lower()

            if answer == "y":
                deleted = 0
                for idx in indices:
                    chat = chats[idx]
                    try:
                        os.remove(chat["file"])
                        deleted += 1
                    except OSError as e:
                        print(f"  {RED}Error: {e}{RESET}")
                    sub = chat["subagent_dir"]
                    if os.path.isdir(sub):
                        shutil.rmtree(sub, ignore_errors=True)
                print()
                print(f"  {GREEN}{BOLD}  Deleted {deleted} {label}.{RESET}")
                print()
                input(f"  {DIM}Press Enter...{RESET}")
            else:
                print(f"\n  {DIM}Cancelled.{RESET}")
                input(f"\n  {DIM}Press Enter...{RESET}")
            # Loop back to reload chat list for this project


if __name__ == "__main__":
    main()
