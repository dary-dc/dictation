# Dictation

Press a key, talk for as long as you want, press it again — your speech lands on
the clipboard as text, ready to paste anywhere. Built for **Fedora / GNOME on
Wayland**, transcribed by **Groq Whisper**.

No more 7-second cap. Speak for 15 seconds or 3 minutes; recording runs until you
stop it.

## What it does

- **Two shortcuts, two moods** (installed by `install-shortcuts.py`):
  **Super + D** — simple mode: plain single-pass Groq, no overlay, fastest
  (`toggle --simple`). **Super + W** — full mode: live overlay + 3-engine
  ensemble + judge (`toggle`). Either key stops a running recording; the
  session always finishes in the mode it was started with.
  **Super + Shift + D** — resend the last recording (re-transcribe or re-copy
  without touching the mic).
- Records continuously to a temp WAV (16 kHz mono), then ships it to Groq's
  `whisper-large-v3-turbo` and puts the result on your clipboard.
- **Live overlay** (on by default): while you speak, a small frameless
  always-on-top window shows the transcription in real time (grey = still
  changing, white = final). Drag it anywhere — it reappears there next time.
  Display only; the clipboard still gets the settled ensemble text on stop.
- **Ensemble mode** (on by default when Deepgram + Gemini keys are available —
  both auto-reused from Bridge/web-agent): the WAV is transcribed by three
  engines in parallel (Groq whisper-large-v3, Groq turbo, Deepgram nova-3),
  hallucinated tails are stripped, and majority wording wins — a Gemini judge
  reconstructs only genuine three-way disagreements. Slow engines are
  abandoned after ~6 s. Set `ensemble = false` for the old instant single pass.
- **Last recording recovery** (`save_last_recording = true`): each dictation
  saves `last.wav` under `~/.local/state/dictation/` before transcribing. If
  transcription or clipboard fails, press **Super + Shift + D** (or
  `dictation.py resend`) to retry — no re-speaking. Transient API errors
  auto-retry twice; clipboard-only retry is instant when a transcript was
  already saved.
- **Quality lab** (`lab = true`): the last 20 dictations keep their audio and
  an engine-by-engine record — run `uv run dictation.py lab` to compare what
  each engine heard vs. what reached your clipboard.
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

This app does **not** pollute your system. No `sudo`, no global `pip install`,
your system Python is never modified.

- Dependencies live in a **project-local** `.venv/` (created by `./setup.sh` or
  `uv sync`). Nothing is installed user-wide or system-wide.
- [`uv`](https://docs.astral.sh/uv/) manages the venv and downloads a private
  CPython 3.13 if needed — kept in `~/.local/share/uv/`, shared with other
  `uv` projects but **not** mixed into your system Python.

Everything that ends up on disk:

| Location | What | Notes |
|---|---|---|
| the cloned repo | The script files + `.venv/` | Delete the folder to remove the app |
| `~/.config/dictation/config.toml` | Your key & settings | Only if you create it (see Setup) |
| `~/.cache/uv/` | Cached dependency wheels | **Shared** `uv` cache, speeds up installs |
| `~/.local/share/uv/python/` | Managed CPython 3.13 | **Shared**, reusable by any `uv` project |
| `$XDG_RUNTIME_DIR/dictation/` | Temp audio while recording | RAM-backed, auto-wiped at logout |
| `~/.local/state/dictation/` | Debug log, recovery slot & optional history | `last.wav` + `last.txt`; `debug.log`; `history.log` if `save_history = true` |
| GNOME dconf | Custom shortcut entries | Only if you run the installer |

See [Uninstall](#uninstall) to remove all of it.

## Requirements

Everything below is already present on a standard Fedora GNOME install; the
commands are here only if `dictation.py doctor` reports something missing.

```bash
sudo dnf install wl-clipboard libnotify portaudio
```

- [`uv`](https://docs.astral.sh/uv/) — install with `curl -LsSf https://astral.sh/uv/install.sh | sh`.
  It creates the project `.venv/`; there is nothing to `pip install` globally.

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

### 4. Install (venv + shortcuts)

```bash
chmod +x setup.sh
./setup.sh
```

This creates `.venv/` in the project, installs dependencies, runs `doctor`, and
registers **Super+D**, **Super+W**, and **Super+Shift+D** (resend). Re-run after
pulling updates. Shortcut-only refresh: `./setup.sh --shortcuts`.

You want `READY ✅` from the doctor step. (`doctor` treats the unedited
`gsk_PASTE...` placeholder as "no key", so it won't give you a false pass.)

### 5. Bind a shortcut (manual alternative)

**Option A — recommended (done by `setup.sh`):**

```bash
python3 install-shortcuts.py
```

This registers **`Super + D`** (simple), **`Super + W`** (full), and
**`Super + Shift + D`** (resend). As *global* GNOME shortcuts they take
priority over app-local bindings. The installer edits shortcuts
non-destructively and won't duplicate on re-run. Pick different keys:

```bash
python3 install-shortcuts.py --simple-key '<Super>z'
```

**Option B — two keys (start / stop), like the original idea:**

```bash
python3 install-shortcuts.py --mode pair
# start = Ctrl+Alt+D, stop = Ctrl+Alt+S
```

**Option C — by hand in the GUI:** *Settings → Keyboard → Keyboard Shortcuts →
View and Customize → Custom Shortcuts → +*

- **Name:** `Dictation: toggle`
- **Command:** `/ABS/PATH/TO/dictation/.venv/bin/python /ABS/PATH/TO/dictation/dictation.py toggle`
  — use absolute paths (GNOME shortcuts run with a minimal `PATH`); get them with
  `realpath dictation.py` and `realpath .venv/bin/python`.
- **Shortcut:** whatever you like — avoid `Ctrl+M` (that's Enter in terminals).

## Your workflow

1. In Cursor, looking at a messy Rust function. Press **`Super + D`** —
   GNOME shows **🎙️ Recording…** (if notifications are enabled; that's your "go" cue).
2. Speak for as long as you need — 15 seconds, 3 minutes — explaining the problem
   in clean spoken English.
3. Press **`Super + D`** again. It transcribes and copies the text.
4. Hit **`Ctrl + V`** where you want the text. Done.

If transcription or clipboard fails after a long dictation, press **`Super + Shift + D`**
to resend the last recording — no need to speak it again.

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

The log is **always** written, whether or not you asked for it:

```
~/.local/state/dictation/debug.log
```

Timestamped lines — device name, captured duration, request timing per engine,
which engines won the vote, transcript length, clipboard tool and verification,
and full tracebacks — rotated at 1 MB (one previous file kept). A hotkey tool
fails when nobody is watching a terminal, and a flag you have to switch on first
is never on at the moment it matters, so the evidence is there before the fact.
Even notifications that the current level *suppresses* are logged.

```bash
uv run dictation.py toggle --debug
```

`--debug` (or `debug = true` in config) adds the same lines on **stderr**, plus
the noisy per-block audio-callback detail that is kept out of the file. To watch
the hotkey path live: `tail -f ~/.local/state/dictation/debug.log`.

### Tests

```bash
python3 test_dictation.py        # no dependencies, no mic, no network
```

Covers the logic that has actually lost a dictation: the PID-file claim that
decides whether a second recorder starts, how long the ensemble waits for its
engines, the majority vote, and the hallucinated-tail stripper. Recording,
uploading and pasting are left to `doctor` and the log — mocking a microphone
would only test the mock.

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
- `save_last_recording` — keep one recovery WAV at `~/.local/state/dictation/last.wav` (default on).

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
- **Resend keeps copying a bad transcript** — delete `~/.local/state/dictation/last.txt`
  and press **Super + Shift + D** again to force a full re-transcription from
  `last.wav`.

## Uninstall

Remove everything this app added:

```bash
python3 install-shortcuts.py --remove                  # from the repo dir — removes the GNOME shortcut
rm -rf ~/.config/dictation ~/.local/state/dictation    # config, logs & history
rm -rf <the cloned dictation folder>                   # the app + its .venv
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
| `dictation.py` | The app — `toggle` / `start` / `stop` / `resend` / `doctor` |
| `pyproject.toml` | Dependencies (managed by `uv sync` into `.venv/`) |
| `setup.sh` | Bootstrap `.venv`, run doctor, install shortcuts |
| `config.example.toml` | Copy to `~/.config/dictation/config.toml` |
| `install-shortcuts.py` | Register/remove GNOME keyboard shortcuts |
| `test_dictation.py` | Tests for the offline logic — `python3 test_dictation.py` |
| `.gitignore` | Keeps a real `config.toml` out of version control |
| `LICENSE` | MIT |

## Manual use (no shortcut)

```bash
.venv/bin/python dictation.py toggle      # or: uv run dictation.py toggle
.venv/bin/python dictation.py resend      # retry last recording after a failure
.venv/bin/python dictation.py doctor      # check your setup
```

## License

MIT — see [LICENSE](LICENSE).
