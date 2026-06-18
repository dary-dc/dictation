# Dictation

Press a key, talk for as long as you want, press it again — your speech lands on
the clipboard as text, ready to paste anywhere. Built for **Fedora / GNOME on
Wayland**, transcribed by **Groq Whisper**.

No more 7-second cap. Speak for 15 seconds or 3 minutes; recording runs until you
stop it.

## What it does

- **`toggle`** — one key (**Super + D** by default): press to start recording, press
  again to stop, transcribe, and copy.
- Records continuously to a temp WAV (16 kHz mono), then ships it to Groq's
  `whisper-large-v3-turbo` and puts the result on your clipboard.
- Desktop notifications (tunable — see [Notifications](#notifications)) tell you
  when it's recording, transcribing, and done.

### Improvements over the two-script version

| Problem in the naïve version | Fix here |
|---|---|
| `pkill` sends SIGTERM but the script only caught SIGINT → WAV header never finalized, risking corrupt/empty audio | Handles SIGTERM/SIGINT and closes the file cleanly |
| API key hard-coded in the source | Read from `~/.config/dictation/config.toml` or `$GROQ_API_KEY` |
| `os.system(f'notify-send "...{text}"')` breaks or runs shell code if the transcript contains `"`, `$`, or backticks | All shell-outs use argument lists — no injection |
| Fragile `pkill -f` process matching | A PID file targets the exact recorder |
| Two scripts, two shortcuts | One script, three modes — bind a single toggle key (or two if you prefer) |
| Always-on notification spam | Tunable levels (`all` / `minimal` / `errors` / `none`) + a debug log |

## Isolation & footprint

This app does **not** pollute your system. No `sudo`, no global installs, your
system Python is never modified.

- `uv` reads the dependency block inside `dictation.py` and builds a **cached,
  isolated virtual environment** on first run, reusing it afterward.
- On a machine with only Python 3.14, `uv` downloads a private CPython 3.13 — kept
  in `uv`'s own directory, shared with any other `uv` script you run.

Everything that ends up on disk:

| Location | What | Notes |
|---|---|---|
| the cloned repo | The script files | The app itself |
| `~/.config/dictation/config.toml` | Your key & settings | Only if you create it (see Setup) |
| `~/.cache/uv/` | Cached dependencies | **Shared** `uv` cache, not app-specific |
| `~/.local/share/uv/python/` | Managed CPython 3.13 | **Shared**, reusable by any `uv` project |
| `$XDG_RUNTIME_DIR/dictation/` | Temp audio while recording | RAM-backed, auto-wiped at logout |
| `~/.local/state/dictation/` | Debug log & optional history | `debug.log`; `history.log` if `save_history = true` |
| GNOME dconf | One custom-shortcut entry | Only if you run the installer |

See [Uninstall](#uninstall) to remove all of it.

## Requirements

Everything below is already present on a standard Fedora GNOME install; the
commands are here only if `dictation.py doctor` reports something missing.

```bash
sudo dnf install wl-clipboard libnotify portaudio
```

- [`uv`](https://docs.astral.sh/uv/) — install with `curl -LsSf https://astral.sh/uv/install.sh | sh`.
  It reads the inline dependency block in `dictation.py`, so there's nothing to `pip install`.

## Setup

### 1. Get the code

```bash
git clone https://github.com/dary-dc/dictation.git
cd dictation
```

### 2. Get a Groq API key

Free at <https://console.groq.com/keys>. It starts with `gsk_`.

### 3. Configure

```bash
mkdir -p ~/.config/dictation
cp config.example.toml ~/.config/dictation/config.toml
nano ~/.config/dictation/config.toml      # paste your gsk_... key
```

> **Why a file and not an env var?** GNOME custom shortcuts run with a minimal
> environment and will **not** see `GROQ_API_KEY` exported in your `~/.bashrc`.
> The config file is read directly by the script, so it always works.

### 4. Verify

```bash
uv run dictation.py doctor
```

You want `READY ✅`. The first run downloads dependencies (a few seconds); after
that it's cached and fast. (`doctor` treats the unedited `gsk_PASTE...` placeholder
as "no key", so it won't give you a false pass.)

### 5. Bind a shortcut

**Option A — one toggle key (recommended):**

```bash
python3 install-shortcuts.py
```

This registers **`Super + D`** to toggle dictation — a comfortable left-hand,
two-finger combo (thumb on Super + middle finger on D). As a *global* GNOME
shortcut it takes priority, so it won't clash with Cursor/VS Code. The installer
edits GNOME's custom shortcuts non-destructively and won't duplicate on re-run.
Pick a different key:

```bash
python3 install-shortcuts.py --toggle-key '<Super>r'
```

**Option B — two keys (start / stop), like the original idea:**

```bash
python3 install-shortcuts.py --mode pair
# start = Ctrl+Alt+D, stop = Ctrl+Alt+S
```

**Option C — by hand in the GUI:** *Settings → Keyboard → Keyboard Shortcuts →
View and Customize → Custom Shortcuts → +*

- **Name:** `Dictation: toggle`
- **Command:** `/ABS/PATH/TO/uv run /ABS/PATH/TO/dictation/dictation.py toggle`
  — use absolute paths (GNOME shortcuts run with a minimal `PATH`); get them with
  `which uv` and `realpath dictation.py`.
- **Shortcut:** whatever you like — avoid `Ctrl+M` (that's Enter in terminals).

## Your workflow

1. In Cursor, looking at a messy Rust function. Press **`Super + D`** —
   GNOME shows **🎙️ Recording…** (if notifications are enabled; that's your "go" cue).
2. Speak for as long as you need — 15 seconds, 3 minutes — explaining the problem
   in clean spoken English.
3. Press **`Super + D`** again. It transcribes and copies the text.
4. Hit **`Ctrl + V`** where you want the text. Done.

## Notifications

Each dictation normally pops three toasts (recording → transcribing → ready). If
you use this constantly, that's noise. Set the level in
`~/.config/dictation/config.toml`:

```toml
notifications = "errors"
```

| Level | Shows |
|---|---|
| `all` | recording, transcribing, done, and problems (default) |
| `minimal` | only the **🎙️ recording** and **📋 ready to paste** cues |
| `errors` | **silent unless something fails** — effectively none during normal use |
| `none` | never notify, even on failure |

Override per run (useful in a custom shortcut command or when debugging):

```bash
uv run dictation.py toggle --notify minimal
uv run dictation.py toggle --quiet          # = --notify none
```

> Trade-off: with `none`/`errors` you lose the "recording started" cue. Since heavy
> use keeps the cache warm, recording starts near-instantly, so this is usually fine
> — but `minimal` is the safer pick if you want that confirmation.

## Debugging

```bash
uv run dictation.py toggle --debug
```

`--debug` (or `debug = true` in config) writes timestamped logs — device name,
captured duration, Groq request timing, transcript length, clipboard tool, and full
tracebacks — to **stderr** and to:

```
~/.local/state/dictation/debug.log
```

Even notifications that the current level *suppresses* are logged, so you can see
exactly what happened. To debug the hotkey path, temporarily add `--debug` to the
shortcut command (Option C above) and `tail -f ~/.local/state/dictation/debug.log`.

### Why it copies instead of typing for you

The text is placed on your clipboard and you paste it with `Ctrl + V` — it does
**not** auto-type into the focused window. On Wayland, GNOME blocks apps from
synthesising keystrokes for security. Real "insert at cursor" would require
[`ydotool`](https://github.com/ReimuNotMoe/ydotool): a background daemon that needs
`uinput`/root permissions and a systemd service — i.e. exactly the kind of
system-wide setup this project avoids. Clipboard + `Ctrl + V` is the clean,
zero-privilege path. (If you later decide the auto-paste is worth it, that hook can
be added — ask.)

## How it works

```
toggle ─┬─ idle?      → record(): claim PID file, capture mic → temp WAV, notify "recording"
        └─ recording? → stop():   SIGTERM the recorder, wait for clean close,
                                   send WAV to Groq, copy text, clean up
```

- Temp audio + PID file live in `$XDG_RUNTIME_DIR/dictation/` (RAM-backed, auto-cleaned).
- `start`/`stop` are the same primitives if you bound two keys instead of one.

## Configuration reference

See `config.example.toml`. Notable options:

- `model` — `whisper-large-v3-turbo` (fast) or `whisper-large-v3` (top quality).
- `language` — `"en"`, another ISO code, or `"auto"` to detect.
- `prompt` — biases Whisper toward your vocabulary (great for code/jargon).
- `notifications` — `all` / `minimal` / `errors` / `none` (see [Notifications](#notifications)).
- `debug` — verbose logging to `~/.local/state/dictation/debug.log`.
- `save_history` — append transcripts to `~/.local/state/dictation/history.log`.

## Troubleshooting

- **Run `uv run dictation.py doctor` first** — it pinpoints most issues and shows
  your current notification level and log path.
- **Something feels off but no notification** — your level may be `errors`/`none`.
  Run once with `--debug` and check `~/.local/state/dictation/debug.log`.
- **"No Groq API key"** — the config file isn't at `~/.config/dictation/config.toml`,
  or the key line is still the `gsk_PASTE...` placeholder.
- **PortAudio / no input device** — `sudo dnf install portaudio`; check your mic in
  *Settings → Sound → Input*.
- **Nothing on the clipboard** — `sudo dnf install wl-clipboard`.
- **First press feels slow** — only the very first `uv run` resolves dependencies;
  run `doctor` once to warm the cache.
- **Shortcut does nothing** — the key combo may already be taken; change it in
  *Settings → Keyboard → Custom Shortcuts*.

## Uninstall

Remove everything this app added:

```bash
python3 install-shortcuts.py --remove                  # from the repo dir — removes the GNOME shortcut
rm -rf ~/.config/dictation ~/.local/state/dictation    # config, logs & history
rm -rf <the cloned dictation folder>                   # the app itself
```

That returns your machine to its prior state. The only remainder is `uv`'s shared
cache and managed Python (`~/.cache/uv`, `~/.local/share/uv`) — these belong to
`uv`, not this app. If you don't use `uv` for anything else and want them gone too:

```bash
uv cache clean
rm -rf ~/.local/share/uv
```

## Files

| File | Purpose |
|---|---|
| `dictation.py` | The app — `toggle` / `start` / `stop` / `doctor` |
| `config.example.toml` | Copy to `~/.config/dictation/config.toml` |
| `install-shortcuts.py` | Register/remove GNOME keyboard shortcuts |
| `.gitignore` | Keeps a real `config.toml` out of version control |
| `LICENSE` | MIT |

## Manual use (no shortcut)

```bash
uv run dictation.py toggle              # press once, run again to stop
uv run dictation.py start               # or drive the two halves yourself
uv run dictation.py stop
uv run dictation.py doctor              # check your setup
uv run dictation.py toggle --quiet      # one-off silent run
uv run dictation.py toggle --debug      # one-off verbose run
```

## License

MIT — see [LICENSE](LICENSE).
