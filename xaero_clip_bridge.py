"""
Xaero Clipboard → Waypoint bridge with Tk GUI
- Watches clipboard for /tp X Y Z, "X: 123 Z: -456", or plain "123 -29 103"
- Appends Xaero-format waypoint lines to a waypoint file
- GUI shows recent copied coordinates and recent waypoint lines
- Toggles: auto-naming, random color, visibility type, disabled, waypoint type
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import queue
import time
import re
import os
import json
import random
import pyperclip
from datetime import datetime
import ctypes

# HiDPI awareness for Windows (fixes blurry Tk on HiDPI displays)
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

# ----------------------------
# Configuration & persistence
# ----------------------------
DEFAULT_SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".xaero_bridge_settings.json")
DEFAULT_WAYPOINT_FILE = os.path.join(os.path.expanduser("~"), "xaero_waypoints.txt")

settings = {
    "waypoint_file": DEFAULT_WAYPOINT_FILE,
    "auto_name": True,
    "name_prefix": "Auto",
    "name_counter": 1,
    "random_color": True,
    "color": 0,
    "visibility_type": 0,
    "disabled": False,
    "wp_type": 0,
    "y_default": 64,
    "append_timestamp_to_name": False,
    "recent_limit": 12,
    "autowrite": True
}

def load_settings():
    try:
        if os.path.exists(DEFAULT_SETTINGS_PATH):
            with open(DEFAULT_SETTINGS_PATH, "r", encoding="utf-8") as f:
                s = json.load(f)
                settings.update(s)
    except Exception as e:
        print("Failed to load settings:", e)

def save_settings():
    try:
        with open(DEFAULT_SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
    except Exception as e:
        print("Failed to save settings:", e)

load_settings()

# ----------------------------
# Regex patterns for coords
# ----------------------------
# Matches: /tp 217 -29 103  OR  tp 217 -29 103
re_tp = re.compile(r"^(?:/)?tp\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)$", re.IGNORECASE)
# Matches: X: 123 Z: -456 (optionally includes Y:)
re_XZ = re.compile(r"X[:=]?\s*(-?\d+)[^\d\-+]+Z[:=]?\s*(-?\d+)", re.IGNORECASE)
# Matches plain "123 -29 103" or "123, -29, 103"
re_plain = re.compile(r"^\s*(-?\d+)[,\s]+\s*(-?\d+)[,\s]+\s*(-?\d+)\s*$")

# Additional constants
POLL_INTERVAL = 1.0  # seconds (used by clipboard watcher)
MAX_HISTORY = settings.get("recent_limit", 12) or 12
DEFAULT_Y = settings.get("y_default", 64)

# Robust /tp parser that handles variants like:
# /tp 100 70 -200   /tp @p 100 70 -200   /tp 100 ~ -200   /tp 100 -200
def parse_tp_command(text: str):
    txt = text.strip()
    if not txt.lower().startswith("/tp"):
        return None

    # Split and remove common tokens
    parts = re.split(r"\s+", txt)
    parts = [p for p in parts if p.lower() not in ["/tp", "@p", "@a", "@r", "@s"]]

    coords = []
    for p in parts:
        if re.fullmatch(r"-?\d+", p) or p == "~":
            coords.append(p)

    # interpret tilde (~)
    for i, val in enumerate(coords):
        if val == "~":
            coords[i] = str(DEFAULT_Y if i == 1 else 0)

    if len(coords) == 2:
        # /tp x z form
        x, z = coords
        y = DEFAULT_Y
    elif len(coords) >= 3:
        x, y, z = coords[:3]
    else:
        return None

    try:
        return int(x), int(y), int(z)
    except ValueError:
        return None

# ----------------------------
# Utility: format waypoint line
# ----------------------------
def generate_waypoint_line(x, y, z, name=None):
    # apply settings
    if settings["auto_name"]:
        cnt = settings.get("name_counter", 1)
        if settings.get("append_timestamp_to_name"):
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            name_val = f"{settings['name_prefix']}{cnt}-{ts}"
        else:
            name_val = f"{settings['name_prefix']}{cnt}"
        settings["name_counter"] = cnt + 1
        save_settings()
    else:
        name_val = name or settings.get("name_prefix", "Auto")
    initials = (name_val[0] if name_val else "A").upper()

    # color
    if settings.get("random_color"):
        color = random.randint(0, 15)
    else:
        color = int(settings.get("color", 0))

    disabled = "true" if settings.get("disabled") else "false"
    wp_type = int(settings.get("wp_type", 0))   # e.g. 0 normal, 2 deathpoint etc.
    visibility_type = int(settings.get("visibility_type", 0))
    # For tp rotate_on_tp and tp_yaw we keep defaults false and 0
    # destination field: false/true? but in sample it's "false" or "true" at end; we'll keep "false"
    line = f"waypoint:{name_val}:{initials}:{x}:{y}:{z}:{color}:{disabled}:{wp_type}:gui.xaero_default:false:0:{visibility_type}:false"
    return line

# ----------------------------
# Clipboard watcher thread
# ----------------------------
clip_q = queue.Queue()
stop_event = threading.Event()

def clipboard_watcher(poll_interval=POLL_INTERVAL):
    last = ""
    while not stop_event.is_set():
        try:
            text = pyperclip.paste()
        except Exception:
            text = ""
        if text and text != last:
            last = text
            clip_q.put(text)
        time.sleep(poll_interval)

# ----------------------------
# Worker: parse clipboard and write waypoint
# ----------------------------
recent_copied = []   # list of (raw_text, parsed_coords or None)
recent_waypoints = []  # list of (line, timestamp)

def process_clip_item(text):
    txt = text.strip()
    # First try the enhanced /tp parser
    parsed = parse_tp_command(txt)
    if parsed:
        return parsed
    # Try X: ... Z:
    m = re_XZ.search(txt)
    if m:
        x, z = m.groups()
        y = settings.get("y_default", DEFAULT_Y)
        return int(x), int(y), int(z)
    # Try plain triple
    m = re_plain.search(txt)
    if m:
        x, y, z = m.groups()
        return int(x), int(y), int(z)
    return None

def append_waypoint_line(line):
    fpath = settings.get("waypoint_file", DEFAULT_WAYPOINT_FILE)
    try:
        # ensure parent dir exists
        os.makedirs(os.path.dirname(fpath), exist_ok=True)
    except Exception:
        pass
    try:
        with open(fpath, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return True, None
    except Exception as e:
        return False, str(e)

def processor_loop(gui_queue):
    while not stop_event.is_set():
        try:
            text = clip_q.get(timeout=0.2)
        except queue.Empty:
            continue
        parsed = process_clip_item(text)
        # add to recent_copied
        recent_copied.insert(0, (text, parsed))
        limit = settings.get("recent_limit", 12)
        recent_copied[:] = recent_copied[:limit]
        gui_queue.put(("update_copied", recent_copied.copy()))
        if parsed and settings.get("autowrite", True):
            x, y, z = parsed
            line = generate_waypoint_line(x, y, z)
            ok, err = append_waypoint_line(line)
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if ok:
                recent_waypoints.insert(0, (line, ts))
                recent_waypoints[:] = recent_waypoints[:limit]
                gui_queue.put(("update_waypoints", recent_waypoints.copy()))
                gui_queue.put(("notify", f"Added waypoint {x},{y},{z}"))
            else:
                gui_queue.put(("error", f"Failed to write waypoint: {err}"))

# ----------------------------
# GUI
# ----------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Xaero Clipboard → Waypoint Bridge")
        self.geometry("800x520")
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.gui_queue = queue.Queue()

        # Top frame: waypoint file chooser and status
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=(8,4))

        ttk.Label(top, text="Waypoint file:").pack(side="left")
        self.file_var = tk.StringVar(value=settings.get("waypoint_file"))
        self.file_entry = ttk.Entry(top, textvariable=self.file_var, width=60)
        self.file_entry.pack(side="left", padx=(6,6))
        ttk.Button(top, text="Choose...", command=self.choose_file).pack(side="left")
        ttk.Button(top, text="Open folder", command=self.open_folder).pack(side="left", padx=(6,0))

        # Middle: left recent copied, right recent waypoints
        middle = ttk.Frame(self)
        middle.pack(fill="both", expand=True, padx=8, pady=6)

        left = ttk.LabelFrame(middle, text="Recent clipboard (raw → parsed)")
        left.pack(side="left", fill="both", expand=True, padx=(0,6))

        self.copied_list = tk.Listbox(left, height=18)
        self.copied_list.pack(fill="both", expand=True, padx=6, pady=6)
        self.copied_list.bind("<Double-Button-1>", self.on_copied_double)

        right = ttk.LabelFrame(middle, text="Recent waypoints written")
        right.pack(side="left", fill="both", expand=True)

        self.wp_list = tk.Listbox(right, height=18)
        self.wp_list.pack(fill="both", expand=True, padx=6, pady=6)
        self.wp_list.bind("<Double-Button-1>", self.on_wp_double)

        # Bottom control panel
        bottom = ttk.Frame(self)
        bottom.pack(fill="x", padx=8, pady=(0,8))

        # Left controls: toggles
        ctrls = ttk.Frame(bottom)
        ctrls.pack(side="left", anchor="w")

        self.autowrite_var = tk.BooleanVar(value=settings.get("autowrite", True))
        ttk.Checkbutton(ctrls, text="Auto-write", variable=self.autowrite_var, command=self.toggle_autowrite).grid(row=0,column=0, sticky="w")

        self.auto_name_var = tk.BooleanVar(value=settings.get("auto_name"))
        ttk.Checkbutton(ctrls, text="Auto-name", variable=self.auto_name_var, command=self.update_setting).grid(row=0,column=1, sticky="w", padx=(8,0))

        ttk.Label(ctrls, text="Prefix:").grid(row=0,column=2, padx=(8,2))
        self.prefix_var = tk.StringVar(value=settings.get("name_prefix"))
        ttk.Entry(ctrls, textvariable=self.prefix_var, width=12).grid(row=0,column=3)

        self.append_ts_var = tk.BooleanVar(value=settings.get("append_timestamp_to_name"))
        ttk.Checkbutton(ctrls, text="TS in name", variable=self.append_ts_var, command=self.update_setting).grid(row=0,column=4, padx=(8,0))

        # Second row: color & visibility
        ttk.Label(ctrls, text="Color:").grid(row=1,column=0, pady=(6,0))
        self.color_spin = tk.Spinbox(ctrls, from_=0, to=15, width=4)
        self.color_spin.delete(0,"end"); self.color_spin.insert(0, str(settings.get("color",0)))
        self.color_spin.grid(row=1,column=1, sticky="w")

        self.random_color_var = tk.BooleanVar(value=settings.get("random_color"))
        ttk.Checkbutton(ctrls, text="Randomize color", variable=self.random_color_var, command=self.update_setting).grid(row=1,column=2, columnspan=2, sticky="w", padx=(8,0))

        ttk.Label(ctrls, text="Visibility:").grid(row=1,column=4, padx=(8,2))
        self.vis_combo = ttk.Combobox(ctrls, values=[0,1,2], width=4)
        self.vis_combo.set(str(settings.get("visibility_type",0)))
        self.vis_combo.grid(row=1,column=5)

        self.disabled_var = tk.BooleanVar(value=settings.get("disabled"))
        ttk.Checkbutton(ctrls, text="Disabled", variable=self.disabled_var, command=self.update_setting).grid(row=1,column=6, padx=(8,0))

        ttk.Label(ctrls, text="Type:").grid(row=1,column=7, padx=(8,2))
        self.type_combo = ttk.Combobox(ctrls, values=[0,1,2], width=4)
        self.type_combo.set(str(settings.get("wp_type",0)))
        self.type_combo.grid(row=1,column=8)

        # Right-side action buttons
        actions = ttk.Frame(bottom)
        actions.pack(side="right", anchor="e")

        ttk.Button(actions, text="Pause", command=self.pause).pack(side="left", padx=(0,8))
        ttk.Button(actions, text="Resume", command=self.resume).pack(side="left", padx=(0,8))
        ttk.Button(actions, text="Copy selected WP line", command=self.copy_selected_wp).pack(side="left", padx=(0,8))
        ttk.Button(actions, text="Clear lists", command=self.clear_lists).pack(side="left")

        # Status bar
        self.status_var = tk.StringVar(value="Idle")
        status = ttk.Label(self, textvariable=self.status_var, relief="sunken", anchor="w")
        status.pack(fill="x", side="bottom")

        # Worker threads are started in main() to avoid duplicate clipboard/process loops
        # (previously this class started clipboard_watcher and processor_loop here,
        # which caused duplicate reads because main() also started them.)

        # Kick off GUI poll
        self.after(200, self.poll_gui_queue)

    # GUI actions
    def choose_file(self):
        p = filedialog.asksaveasfilename(
            title="Choose waypoint file (existing files will be appended)",
            defaultextension=".txt",
            filetypes=[("Text files","*.txt"),("All files","*.*")],
            initialfile=os.path.basename(settings.get("waypoint_file", DEFAULT_WAYPOINT_FILE)),
            initialdir=os.path.dirname(settings.get("waypoint_file", DEFAULT_WAYPOINT_FILE)) or os.path.expanduser("~")
        )
        if p:
            settings["waypoint_file"] = p
            self.file_var.set(p)
            save_settings()
            self.status_var.set(f"Waypoint file set: {p}")

    def open_folder(self):
        p = settings.get("waypoint_file", DEFAULT_WAYPOINT_FILE)
        folder = os.path.dirname(os.path.abspath(p))
        try:
            if os.name == "nt":
                os.startfile(folder)
            else:
                # mac / linux
                import subprocess
                subprocess.Popen(["xdg-open" if os.name == "posix" else "open", folder])
        except Exception:
            messagebox.showinfo("Open folder", f"Waypoint file folder: {folder}")

    def pause(self):
        settings["autowrite"] = False
        self.autowrite_var.set(False)
        save_settings()
        self.status_var.set("Auto-write paused")

    def resume(self):
        settings["autowrite"] = True
        self.autowrite_var.set(True)
        save_settings()
        self.status_var.set("Auto-write resumed")

    def toggle_autowrite(self):
        settings["autowrite"] = bool(self.autowrite_var.get())
        save_settings()
        self.status_var.set("Auto-write: " + ("On" if settings["autowrite"] else "Off"))

    def update_setting(self):
        # write UI values into settings
        settings["auto_name"] = bool(self.auto_name_var.get())
        settings["name_prefix"] = self.prefix_var.get() or "Auto"
        settings["append_timestamp_to_name"] = bool(self.append_ts_var.get())
        settings["random_color"] = bool(self.random_color_var.get())
        try:
            settings["color"] = int(self.color_spin.get())
        except Exception:
            settings["color"] = 0
        try:
            settings["visibility_type"] = int(self.vis_combo.get())
        except Exception:
            settings["visibility_type"] = 0
        try:
            settings["wp_type"] = int(self.type_combo.get())
        except Exception:
            settings["wp_type"] = 0
        settings["disabled"] = bool(self.disabled_var.get())
        settings["waypoint_file"] = self.file_var.get() or settings.get("waypoint_file")
        save_settings()
        self.status_var.set("Settings updated")

    def poll_gui_queue(self):
        # update settings from UI first
        self.update_setting()
        while True:
            try:
                cmd, payload = self.gui_queue.get_nowait()
            except queue.Empty:
                break
            if cmd == "update_copied":
                self.refresh_copied(payload)
            elif cmd == "update_waypoints":
                self.refresh_waypoints(payload)
            elif cmd == "notify":
                self.status_var.set(payload)
            elif cmd == "error":
                messagebox.showerror("Error", payload)
        # Also copy from recent globals if changed by other thread
        # (processor_loop already pushes updates into gui_queue)
        self.after(200, self.poll_gui_queue)

    def refresh_copied(self, items):
        self.copied_list.delete(0, tk.END)
        for raw, parsed in items:
            if parsed:
                s = f"{raw}  →  {parsed[0]},{parsed[1]},{parsed[2]}"
            else:
                s = f"{raw}  →  (no coords)"
            self.copied_list.insert(tk.END, s)

    def refresh_waypoints(self, items):
        self.wp_list.delete(0, tk.END)
        for line, ts in items:
            display = f"{ts}  {line}"
            self.wp_list.insert(tk.END, display)

    def on_copied_double(self, event):
        sel = self.copied_list.curselection()
        if not sel:
            return
        idx = sel[0]
        raw, parsed = recent_copied[idx]
        if parsed:
            x,y,z = parsed
            # show a small dialog to allow manual writing
            if messagebox.askyesno("Write waypoint?", f"Write waypoint at {x},{y},{z} to file now?"):
                line = generate_waypoint_line(x,y,z)
                ok, err = append_waypoint_line(line)
                if ok:
                    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    recent_waypoints.insert(0, (line, ts))
                    self.refresh_waypoints(recent_waypoints)
                    self.status_var.set(f"Wrote waypoint {x},{y},{z}")
                else:
                    messagebox.showerror("Write failed", f"Failed to write waypoint: {err}")
        else:
            messagebox.showinfo("No coordinates", "Selected clipboard entry does not contain parseable coordinates.")

    def on_wp_double(self, event):
        sel = self.wp_list.curselection()
        if not sel:
            return
        idx = sel[0]
        line, ts = recent_waypoints[idx]
        # copy the line to clipboard
        pyperclip.copy(line)
        self.status_var.set("Waypoint line copied to clipboard")

    def copy_selected_wp(self):
        sel = self.wp_list.curselection()
        if not sel:
            messagebox.showinfo("No selection", "Select a waypoint in the right list to copy.")
            return
        idx = sel[0]
        line, ts = recent_waypoints[idx]
        pyperclip.copy(line)
        self.status_var.set("Waypoint line copied to clipboard")

    def clear_lists(self):
        recent_copied.clear()
        recent_waypoints.clear()
        self.copied_list.delete(0, tk.END)
        self.wp_list.delete(0, tk.END)
        self.status_var.set("Lists cleared")

    def on_close(self):
        if messagebox.askokcancel("Quit", "Quit Xaero Clipboard Bridge?"):
            stop_event.set()
            save_settings()
            self.destroy()

# ----------------------------
# main
# ----------------------------
def main():
    gui_q = queue.Queue()
    # Start processor thread that watches clip_q and writes files, sends gui updates
    proc = threading.Thread(target=processor_loop, args=(gui_q,), daemon=True)
    proc.start()

    # Start clipboard watcher thread
    clip = threading.Thread(target=clipboard_watcher, daemon=True)
    clip.start()

    # Bridge threads to Tk main app by transferring gui_q items into app.gui_queue
    app = App()

    # attach background transfer: read from local gui_q and put into app.gui_queue
    def forward_gui_q():
        while True:
            try:
                item = gui_q.get_nowait()
            except queue.Empty:
                break
            app.gui_queue.put(item)
        if not stop_event.is_set():
            app.after(150, forward_gui_q)

    app.after(150, forward_gui_q)
    app.mainloop()

if __name__ == "__main__":
    main()
