#!/usr/bin/env python3
"""Register GNOME custom keyboard shortcuts for dictation (no extra deps).

It edits the dconf custom-keybindings list *non-destructively*: existing
shortcuts are kept, and re-running updates the dictation entries in place
instead of creating duplicates.

    python3 install-shortcuts.py                 # one toggle key (default)
    python3 install-shortcuts.py --mode pair     # separate start + stop keys
    python3 install-shortcuts.py --toggle-key '<Super>z'
    python3 install-shortcuts.py --remove        # remove dictation shortcuts

Default bindings (change anytime in Settings → Keyboard → Custom Shortcuts):
    toggle : <Super>d        (left-hand, two-finger: Super + D)
    pair   : start <Control><Alt>d   stop <Control><Alt>s

Note: <Control>m is intentionally avoided — in terminals it is the Enter key.
"""

from __future__ import annotations

import argparse
import ast
import shutil
import subprocess
import sys
from pathlib import Path

SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
LIST_KEY = "custom-keybindings"
ITEM_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"
BASE = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings"


def gset_get(schema: str, key: str, path: str | None = None) -> str:
    target = f"{schema}:{path}" if path else schema
    return subprocess.check_output(["gsettings", "get", target, key], text=True).strip()


def gset_set(schema: str, key: str, value: str, path: str | None = None) -> None:
    target = f"{schema}:{path}" if path else schema
    subprocess.run(["gsettings", "set", target, key, value], check=True)


def get_list() -> list[str]:
    raw = gset_get(SCHEMA, LIST_KEY)
    if raw.startswith("@as") or raw in ("[]", ""):
        return []
    try:
        return list(ast.literal_eval(raw))
    except (ValueError, SyntaxError):
        return []


def item_command(path: str) -> str:
    try:
        return ast.literal_eval(gset_get(ITEM_SCHEMA, "command", path))
    except Exception:  # noqa: BLE001
        return ""


def free_path(existing: list[str]) -> str:
    i = 0
    while f"{BASE}/custom{i}/" in existing:
        i += 1
    return f"{BASE}/custom{i}/"


def upsert(name: str, command: str, binding: str) -> None:
    """Create or update the custom shortcut whose command matches `command`."""
    paths = get_list()
    path = next((p for p in paths if item_command(p) == command), None)
    if path is None:
        path = free_path(paths)
        paths.append(path)
        gset_set(SCHEMA, LIST_KEY, repr(paths))
    gset_set(ITEM_SCHEMA, "name", name, path)
    gset_set(ITEM_SCHEMA, "command", command, path)
    gset_set(ITEM_SCHEMA, "binding", binding, path)
    print(f"  • {name!r}  [{binding}]  →  {command}")


def remove(commands: list[str]) -> None:
    paths = get_list()
    keep = [p for p in paths if item_command(p) not in commands]
    removed = len(paths) - len(keep)
    gset_set(SCHEMA, LIST_KEY, repr(keep) if keep else "@as []")
    print(f"Removed {removed} dictation shortcut(s).")


def main() -> int:
    if not shutil.which("gsettings"):
        sys.exit("gsettings not found — this installer is for GNOME only.")

    uv = shutil.which("uv") or str(Path.home() / ".local/bin/uv")
    script = str(Path(__file__).resolve().parent / "dictation.py")

    cmd_toggle = f"{uv} run {script} toggle"
    cmd_start = f"{uv} run {script} start"
    cmd_stop = f"{uv} run {script} stop"

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["toggle", "pair"], default="toggle")
    ap.add_argument("--toggle-key", default="<Super>d")
    ap.add_argument("--start-key", default="<Control><Alt>d")
    ap.add_argument("--stop-key", default="<Control><Alt>s")
    ap.add_argument("--remove", action="store_true", help="remove dictation shortcuts and exit")
    args = ap.parse_args()

    if args.remove:
        remove([cmd_toggle, cmd_start, cmd_stop])
        return 0

    if args.mode == "toggle":
        print("Installing single toggle shortcut:")
        upsert("Dictation: toggle", cmd_toggle, args.toggle_key)
    else:
        print("Installing start + stop shortcuts:")
        upsert("Dictation: start", cmd_start, args.start_key)
        upsert("Dictation: stop", cmd_stop, args.stop_key)

    print("\nDone. The shortcut is live now (no logout needed).")
    print("Change or clear it in Settings → Keyboard → Keyboard Shortcuts → Custom Shortcuts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
