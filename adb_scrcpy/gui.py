"""Portable-friendly Tkinter dashboard for Android device management."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import queue
import re
import shlex
import sys
import time
import tkinter as tk
from io import BytesIO
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Sequence

from .adb_client import AdbClient
from .config import RuntimeConfig
from .device_manager import Device, DeviceManager
from .scrcpy_manager import ScrcpyManager
from .safety import SafetyController
from .workflow import WorkflowContext, WorkflowResult, WorkflowRunner
from .workflow_queue import QueueControl, QueueItem, WorkflowQueue
from .workflow_spec import build_steps, has_mutating_actions, load_spec
from .device_health import DeviceHealthMonitor
from .geometry import GeometryProvider
from .recorder import MouseGesture, MouseRecorder, RecordedWorkflow


POLL_MS = 2_000
SCRCPY_POLL_MS = 500
RECORDER_POLL_MS = 16


@dataclass(frozen=True)
class DeviceDetails:
    device: Device
    android_version: str = "-"
    sdk: str = "-"
    geometry: str = "-"


def app_directory() -> Path:
    """Use the executable directory when frozen, otherwise the repository root."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def state_label(state: str) -> str:
    return {
        "device": "Ready",
        "offline": "Offline",
        "unauthorized": "Authorize USB debugging",
        "no permissions": "No permissions",
    }.get(state, "Unknown")


class DeviceDashboard(tk.Tk):
    """Native desktop UI; ADB work is kept off Tk's event loop."""

    def __init__(self, config_path: str | Path | None = None) -> None:
        super().__init__()
        self.title("Python ADB Controller")
        self.minsize(980, 720)
        self.geometry("1180x840")
        self.configure(bg="#f4f6f8")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._events: queue.Queue[Callable[[], None]] = queue.Queue()
        self._closing = False
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="adb-ui")
        self._poll_pending = False
        self._known_devices: dict[str, Device] = {}
        self._last_seen: dict[str, float] = {}
        self._device_meta: dict[str, dict[str, object]] = {}
        self._selected_serials: set[str] = set()
        self.filter_state_var = tk.StringVar(value="All")
        self.filter_model_var = tk.StringVar(value="All")
        self.filter_tag_var = tk.StringVar(value="")
        self.filter_android_var = tk.StringVar(value="All")
        self.filter_app_var = tk.StringVar(value="")
        self._grid_foreground: dict[str, str] = {}
        self._grid_android: dict[str, str] = {}
        self._grid_pending: set[str] = set()
        self._grid_images: dict[str, object] = {}
        self._current_serial: str | None = None
        self._opening_scrcpy: set[str] = set()
        self._active_scrcpy_serials: set[str] = set()
        self._game_safe_mode = False
        self._workflow_context: WorkflowContext | None = None
        self._workflow_queue: WorkflowQueue | None = None
        self.safety = SafetyController()
        self._safety_latched = False
        self._workflow_step_ids: dict[str, str] = {}
        self._workflow_loop_index = 1
        self._workflow_loop_total = 1
        self._workflow_started_at: float | None = None
        self._recorder: MouseRecorder | None = None
        self._recorded_workflow: RecordedWorkflow | None = None
        self._recorder_steps: list[dict[str, object]] = []
        self._recorder_selected_index: int | None = None
        default_config = app_directory() / "config" / "config.toml" if getattr(sys, "frozen", False) else app_directory() / "adb_scrcpy" / "config.example.toml"
        self.config_path = Path(config_path) if config_path else default_config
        self.config = RuntimeConfig.from_toml(self.config_path)
        self._device_meta = self._read_device_metadata()
        self._saved_ui_state = self._read_ui_state()
        saved_geometry = self._saved_ui_state.get("window_geometry")
        if isinstance(saved_geometry, str) and saved_geometry:
            try:
                self.geometry(saved_geometry)
            except tk.TclError:
                pass
        self.device_manager = DeviceManager(self.config.adb_path, command_timeout=self.config.command_timeout)
        self.scrcpy_manager = ScrcpyManager(self.config.scrcpy_path)
        self._build_styles()
        self._build_ui()
        self._restore_preferences()
        self._apply_theme()
        saved_workflow = self._saved_ui_state.get("workflow_path")
        if isinstance(saved_workflow, str) and Path(saved_workflow).exists():
            self.workflow_path.set(saved_workflow)
            self._load_editor_workflow(silent=True)
        saved_tab = self._saved_ui_state.get("tab_index")
        if isinstance(saved_tab, int) and 0 <= saved_tab < len(self.tabs.tabs()):
            self.tabs.select(saved_tab)
        self._set_status("Scanning for USB devices...")
        self.after(50, self._drain_events)
        self.after(100, self._poll_devices)
        self.after(SCRCPY_POLL_MS, self._poll_scrcpy_sessions)
        self.after(250, self._poll_grid_thumbnails)

    def _build_styles(self) -> None:
        style = ttk.Style(self)
        self._style = style
        style.theme_use("clam")
        style.configure("App.TFrame", background="#f4f6f8")
        style.configure("Panel.TFrame", background="#ffffff", relief="solid", borderwidth=1)
        style.configure("Header.TLabel", background="#f4f6f8", foreground="#1d2733", font=("Segoe UI", 18, "bold"))
        style.configure("Subtle.TLabel", background="#f4f6f8", foreground="#5c6875", font=("Segoe UI", 9))
        style.configure("PanelTitle.TLabel", background="#ffffff", foreground="#1d2733", font=("Segoe UI", 11, "bold"))
        style.configure("Value.TLabel", background="#ffffff", foreground="#1d2733", font=("Segoe UI", 10))
        style.configure("Sidebar.TFrame", background="#eef2f6")
        style.configure("SidebarCaption.TLabel", background="#eef2f6", foreground="#5c6875", font=("Segoe UI", 9, "bold"))
        style.configure("SidebarStatus.TLabel", background="#eef2f6", foreground="#5c6875", font=("Segoe UI", 9))
        style.configure("Nav.TButton", background="#eef2f6", foreground="#314052", anchor="w", padding=(12, 9), font=("Segoe UI", 10))
        style.configure("NavActive.TButton", background="#dceeff", foreground="#175ea8", anchor="w", padding=(12, 9), font=("Segoe UI", 10, "bold"))
        style.configure("Ready.TLabel", background="#dff4e7", foreground="#166534", padding=(8, 4), font=("Segoe UI", 9, "bold"))
        style.configure("Warning.TLabel", background="#fff2cc", foreground="#8a5a00", padding=(8, 4), font=("Segoe UI", 9, "bold"))
        style.configure("Error.TLabel", background="#fde2e1", foreground="#b42318", padding=(8, 4), font=("Segoe UI", 9, "bold"))
        style.configure("Danger.TButton", background="#b42318", foreground="#ffffff", padding=(12, 9), font=("Segoe UI", 9, "bold"))
        style.configure("Info.TLabel", background="#dceeff", foreground="#175ea8", padding=(8, 4), font=("Segoe UI", 9, "bold"))
        style.configure("Primary.TButton", padding=(14, 9), font=("Segoe UI", 10, "bold"))
        style.configure("Secondary.TButton", padding=(12, 9), font=("Segoe UI", 10))
        style.configure("Treeview", rowheight=32, font=("Segoe UI", 10), background="#ffffff", fieldbackground="#ffffff")
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"), background="#eaf0f5", foreground="#324152")
        try:
            style.layout("Hidden.TNotebook.Tab", [])
        except tk.TclError:
            pass

    def _theme_palette(self) -> dict[str, str]:
        dark = getattr(self, "theme_var", None) is not None and self.theme_var.get() == "Dark"
        if dark:
            return {
                "app": "#1c1c1e", "surface": "#2c2c2e", "sidebar": "#242426", "text": "#f5f5f7",
                "muted": "#b8b8c0", "border": "#48484a", "input": "#3a3a3c", "accent": "#0a84ff",
                "info_bg": "#173b5c", "info_fg": "#9dccff", "ready_bg": "#193d2b", "ready_fg": "#8de6ad",
                "warning_bg": "#4b3713", "warning_fg": "#ffd166", "error_bg": "#4b2020", "error_fg": "#ff9f9f",
            }
        accent = self.accent_var.get().strip() if hasattr(self, "accent_var") else "#007AFF"
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", accent):
            accent = "#007AFF"
        return {
            "app": "#f5f5f7", "surface": "#ffffff", "sidebar": "#eef2f6", "text": "#1d2733",
            "muted": "#5c6875", "border": "#c7c7cc", "input": "#ffffff", "accent": accent,
            "info_bg": "#dceeff", "info_fg": "#175ea8", "ready_bg": "#dff4e7", "ready_fg": "#166534",
            "warning_bg": "#fff2cc", "warning_fg": "#8a5a00", "error_bg": "#fde2e1", "error_fg": "#b42318",
        }

    def _apply_theme(self) -> None:
        if not hasattr(self, "_style"):
            return
        palette = self._theme_palette()
        style = self._style
        self.configure(bg=palette["app"])
        style.configure("App.TFrame", background=palette["app"])
        style.configure("Panel.TFrame", background=palette["surface"], bordercolor=palette["border"])
        style.configure("Header.TLabel", background=palette["app"], foreground=palette["text"])
        style.configure("Subtle.TLabel", background=palette["app"], foreground=palette["muted"])
        style.configure("PanelTitle.TLabel", background=palette["surface"], foreground=palette["text"])
        style.configure("Value.TLabel", background=palette["surface"], foreground=palette["text"])
        style.configure("Sidebar.TFrame", background=palette["sidebar"])
        style.configure("SidebarCaption.TLabel", background=palette["sidebar"], foreground=palette["muted"])
        style.configure("SidebarStatus.TLabel", background=palette["sidebar"], foreground=palette["muted"])
        style.configure("Nav.TButton", background=palette["sidebar"], foreground=palette["text"])
        style.configure("NavActive.TButton", background=palette["info_bg"], foreground=palette["accent"])
        style.map("Nav.TButton", background=[("active", palette["info_bg"]), ("focus", palette["info_bg"])])
        style.map("NavActive.TButton", background=[("active", palette["info_bg"]), ("focus", palette["info_bg"])])
        style.configure("Info.TLabel", background=palette["info_bg"], foreground=palette["info_fg"])
        style.configure("Ready.TLabel", background=palette["ready_bg"], foreground=palette["ready_fg"])
        style.configure("Warning.TLabel", background=palette["warning_bg"], foreground=palette["warning_fg"])
        style.configure("Error.TLabel", background=palette["error_bg"], foreground=palette["error_fg"])
        style.configure("Treeview", background=palette["surface"], fieldbackground=palette["surface"], foreground=palette["text"])
        style.configure("Treeview.Heading", background=palette["sidebar"], foreground=palette["text"])
        style.configure("TButton", background=palette["surface"], foreground=palette["text"], bordercolor=palette["border"])
        style.map("TButton", background=[("active", palette["info_bg"]), ("focus", palette["info_bg"])], foreground=[("disabled", palette["muted"])])
        style.configure("TEntry", fieldbackground=palette["input"], foreground=palette["text"], bordercolor=palette["border"])
        style.configure("TCombobox", fieldbackground=palette["input"], foreground=palette["text"], bordercolor=palette["border"])
        style.configure("TSpinbox", fieldbackground=palette["input"], foreground=palette["text"], bordercolor=palette["border"])
        style.configure("TCheckbutton", background=palette["app"], foreground=palette["text"])
        style.map("TCheckbutton", background=[("active", palette["info_bg"]), ("focus", palette["info_bg"])])
        style.configure("TLabelframe", background=palette["surface"], foreground=palette["text"], bordercolor=palette["border"])
        style.configure("TLabelframe.Label", background=palette["surface"], foreground=palette["text"])
        style.configure("TNotebook", background=palette["app"], bordercolor=palette["border"])
        style.configure("TNotebook.Tab", background=palette["sidebar"], foreground=palette["text"])
        self._restyle_plain_widgets(self, palette)
        if hasattr(self, "sidebar_status"):
            self.sidebar_status.configure(text="ADB paused" if self._game_safe_mode else "ADB active")
        if hasattr(self, "_sidebar_routes"):
            self._sync_sidebar_route()

    def _restyle_plain_widgets(self, parent: tk.Misc, palette: dict[str, str]) -> None:
        for child in parent.winfo_children():
            try:
                widget_class = child.winfo_class()
                if widget_class in {"Text", "Listbox"}:
                    child.configure(background=palette["input"], foreground=palette["text"], insertbackground=palette["text"])
                elif widget_class == "Canvas":
                    child.configure(background=palette["surface"])
                elif widget_class == "Label" and child not in {getattr(self, "focus_image_label", None)}:
                    child.configure(background=palette["surface"], foreground=palette["text"])
            except (tk.TclError, AttributeError):
                pass
            self._restyle_plain_widgets(child, palette)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, style="App.TFrame", padding=18)
        root.pack(fill="both", expand=True)

        header = ttk.Frame(root, style="App.TFrame")
        header.pack(fill="x")
        ttk.Label(header, text="Python ADB Controller", style="Header.TLabel").pack(side="left")
        ttk.Label(header, text="USB device console", style="Subtle.TLabel").pack(side="left", padx=(12, 0), pady=(8, 0))
        self.quick_record_button = ttk.Button(header, text="Record", style="Secondary.TButton", command=self._start_recording, state="disabled")
        self.quick_record_button.pack(side="right", padx=(8, 0))
        self.emergency_button = ttk.Button(header, text="Emergency stop", style="Danger.TButton", command=self._emergency_stop)
        self.emergency_button.pack(side="right", padx=(8, 0))
        self.game_safe_button = ttk.Button(header, text="Game safe mode", style="Secondary.TButton", command=self._enter_game_safe_mode)
        self.game_safe_button.pack(side="right", padx=(8, 0))
        self.reset_safety_button = ttk.Button(header, text="Reset safety", style="Secondary.TButton", command=self._reset_safety, state="disabled")
        self.reset_safety_button.pack(side="right", padx=(8, 0))
        ttk.Button(header, text="Refresh", style="Secondary.TButton", command=self._poll_devices).pack(side="right")
        ttk.Button(header, text="Open artifacts", style="Secondary.TButton", command=self._open_artifacts).pack(side="right", padx=(8, 0))

        summary = ttk.Frame(root, style="App.TFrame")
        summary.pack(fill="x", pady=(14, 12))
        self.summary_count = ttk.Label(summary, text="0 devices", style="Info.TLabel")
        self.summary_count.pack(side="left")
        self.summary_state = ttk.Label(summary, text="Scanning", style="Info.TLabel")
        self.summary_state.pack(side="left", padx=(8, 0))
        ttk.Label(summary, text=f"ADB: {self.config.adb_path}", style="Subtle.TLabel").pack(side="right", pady=(5, 0))

        shell = ttk.Frame(root, style="App.TFrame")
        shell.pack(fill="both", expand=True)
        sidebar = ttk.Frame(shell, style="Sidebar.TFrame", padding=(10, 12))
        sidebar.pack(side="left", fill="y", padx=(0, 12))
        ttk.Label(sidebar, text="CONTROLLER", style="SidebarCaption.TLabel").pack(anchor="w", padx=(8, 8), pady=(2, 12))
        self._sidebar_buttons: dict[str, ttk.Button] = {}
        self._sidebar_routes: dict[str, object] = {}
        content_host = ttk.Frame(shell, style="App.TFrame")
        content_host.pack(side="left", fill="both", expand=True)
        tabs = ttk.Notebook(content_host, style="Hidden.TNotebook")
        tabs.pack(fill="both", expand=True)
        devices_tab = ttk.Frame(tabs, style="App.TFrame", padding=10)
        adb_tab = ttk.Frame(tabs, style="App.TFrame", padding=16)
        workflow_tab = ttk.Frame(tabs, style="App.TFrame", padding=16)
        recorder_tab = ttk.Frame(tabs, style="App.TFrame", padding=16)
        logs_tab = ttk.Frame(tabs, style="App.TFrame", padding=16)
        editor_tab = ttk.Frame(tabs, style="App.TFrame", padding=16)
        focus_tab = ttk.Frame(tabs, style="App.TFrame", padding=16)
        settings_tab = ttk.Frame(tabs, style="App.TFrame", padding=16)
        files_tab = ttk.Frame(tabs, style="App.TFrame", padding=16)
        tabs.add(devices_tab, text="Devices")
        tabs.add(adb_tab, text="ADB Control")
        tabs.add(workflow_tab, text="Workflow")
        tabs.add(recorder_tab, text="Recorder")
        tabs.add(logs_tab, text="Logs")
        tabs.add(editor_tab, text="Editor")
        tabs.add(focus_tab, text="Focus")
        tabs.add(settings_tab, text="Settings")
        tabs.add(files_tab, text="Files & APK")
        self.tabs = tabs
        self.focus_tab = focus_tab
        self.settings_tab = settings_tab
        self.files_tab = files_tab
        self._sidebar_routes = {
            "Dashboard": devices_tab,
            "Devices": devices_tab,
            "Automation": workflow_tab,
            "Files & APK": files_tab,
            "Recorder": recorder_tab,
            "Logs": logs_tab,
            "Settings": settings_tab,
        }
        for label in self._sidebar_routes:
            button = ttk.Button(sidebar, text=label, style="Nav.TButton", command=lambda route=label: self._select_route(route))
            button.pack(fill="x", pady=2)
            self._sidebar_buttons[label] = button
        ttk.Separator(sidebar, orient="horizontal").pack(fill="x", padx=8, pady=(16, 10))
        self.sidebar_status = ttk.Label(sidebar, text="ADB active", style="SidebarStatus.TLabel", wraplength=160)
        self.sidebar_status.pack(side="bottom", anchor="w", padx=8, pady=(8, 2))
        tabs.bind("<<NotebookTabChanged>>", self._on_route_changed)

        body = ttk.Panedwindow(devices_tab, orient="horizontal")
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body, style="Panel.TFrame", padding=14)
        right = ttk.Frame(body, style="Panel.TFrame", padding=18)
        body.add(left, weight=4)
        body.add(right, weight=6)

        device_header = ttk.Frame(left, style="Panel.TFrame")
        device_header.pack(fill="x")
        ttk.Label(device_header, text="Connected Devices", style="PanelTitle.TLabel").pack(side="left")
        self.devices_expanded = True
        self.device_toggle = ttk.Button(device_header, text="Collapse", style="Secondary.TButton", command=self._toggle_devices)
        self.device_toggle.pack(side="right")
        filters = ttk.Frame(left, style="Panel.TFrame")
        filters.pack(fill="x", pady=(8, 0))
        ttk.Label(filters, text="State", style="Subtle.TLabel").pack(side="left")
        self.filter_state = ttk.Combobox(filters, textvariable=self.filter_state_var, values=("All", "Ready", "Offline", "Authorize USB debugging", "No permissions"), state="readonly", width=18)
        self.filter_state.pack(side="left", padx=(5, 10))
        ttk.Label(filters, text="Model", style="Subtle.TLabel").pack(side="left")
        self.filter_model = ttk.Combobox(filters, textvariable=self.filter_model_var, values=("All",), state="readonly", width=15)
        self.filter_model.pack(side="left", padx=(5, 10))
        ttk.Label(filters, text="Android", style="Subtle.TLabel").pack(side="left")
        self.filter_android = ttk.Combobox(filters, textvariable=self.filter_android_var, values=("All",), state="readonly", width=10)
        self.filter_android.pack(side="left", padx=(5, 10))
        ttk.Label(filters, text="Tag", style="Subtle.TLabel").pack(side="left")
        self.filter_tag = ttk.Entry(filters, textvariable=self.filter_tag_var, width=14)
        self.filter_tag.pack(side="left", padx=(5, 0))
        self.filter_app = ttk.Entry(filters, textvariable=self.filter_app_var, width=14)
        self.filter_app.pack(side="left", padx=(5, 0))
        self.filter_state.bind("<<ComboboxSelected>>", lambda _event: self._render_device_grid())
        self.filter_model.bind("<<ComboboxSelected>>", lambda _event: self._render_device_grid())
        self.filter_android.bind("<<ComboboxSelected>>", lambda _event: self._render_device_grid())
        self.filter_tag.bind("<KeyRelease>", lambda _event: self._render_device_grid())
        self.filter_app.bind("<KeyRelease>", lambda _event: self._render_device_grid())
        scope = ttk.Frame(left, style="Panel.TFrame")
        scope.pack(fill="x", pady=(8, 0))
        self.selection_summary = ttk.Label(scope, text="0 selected", style="Info.TLabel")
        self.selection_summary.pack(side="left")
        ttk.Button(scope, text="Select visible", style="Secondary.TButton", command=self._select_visible_devices).pack(side="left", padx=(6, 0))
        ttk.Button(scope, text="Clear", style="Secondary.TButton", command=self._clear_selection).pack(side="left", padx=(6, 0))
        ttk.Button(scope, text="Batch health", style="Secondary.TButton", command=self._batch_health).pack(side="right")
        ttk.Button(scope, text="Batch screenshots", style="Secondary.TButton", command=self._batch_screenshot).pack(side="right", padx=(0, 6))
        self.device_list_frame = ttk.Frame(left, style="Panel.TFrame")
        self.device_list_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(self.device_list_frame, columns=("name", "state", "model", "last_seen"), show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="Serial")
        self.tree.heading("name", text="Name")
        self.tree.heading("state", text="State")
        self.tree.heading("model", text="Model")
        self.tree.heading("last_seen", text="Last seen")
        self.tree.column("#0", width=150, stretch=True)
        self.tree.column("name", width=120, stretch=True)
        self.tree.column("state", width=90, stretch=False)
        self.tree.column("model", width=120, stretch=True)
        self.tree.column("last_seen", width=90, stretch=False)
        self.tree.pack_forget()
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.grid_canvas = tk.Canvas(self.device_list_frame, background="#ffffff", highlightthickness=0)
        self._grid_canvas_width = 0
        self.grid_scroll = ttk.Scrollbar(self.device_list_frame, orient="vertical", command=self.grid_canvas.yview)
        self.grid_canvas.configure(yscrollcommand=self.grid_scroll.set)
        self.grid_scroll.pack(side="right", fill="y")
        self.grid_canvas.pack(side="left", fill="both", expand=True, pady=(10, 12))
        self.grid_inner = ttk.Frame(self.grid_canvas, style="Panel.TFrame")
        self.grid_window = self.grid_canvas.create_window((0, 0), window=self.grid_inner, anchor="nw")
        self.grid_inner.bind("<Configure>", lambda _event: self.grid_canvas.configure(scrollregion=self.grid_canvas.bbox("all")))
        self.grid_canvas.bind("<Configure>", self._on_grid_canvas_configure)

        actions = ttk.Frame(left, style="Panel.TFrame")
        actions.pack(fill="x")
        self.check_button = ttk.Button(actions, text="Check Device", style="Primary.TButton", command=self._check_selected, state="disabled")
        self.check_button.pack(fill="x")
        self.open_button = ttk.Button(actions, text="Open scrcpy", style="Secondary.TButton", command=self._open_scrcpy, state="disabled")
        self.open_button.pack(fill="x", pady=(8, 0))
        self.stop_button = ttk.Button(actions, text="Stop scrcpy", style="Secondary.TButton", command=self._stop_scrcpy, state="disabled")
        self.stop_button.pack(fill="x", pady=(8, 0))
        self.reconnect_button = ttk.Button(actions, text="Reconnect ADB", style="Secondary.TButton", command=self._reconnect_adb)
        self.reconnect_button.pack(fill="x", pady=(8, 0))
        self.metadata_button = ttk.Button(actions, text="Edit name & tags", style="Secondary.TButton", command=self._edit_device_metadata, state="disabled")
        self.metadata_button.pack(fill="x", pady=(8, 0))
        ttk.Separator(actions, orient="horizontal").pack(fill="x", pady=12)
        ttk.Label(actions, text="scrcpy Session", style="PanelTitle.TLabel").pack(anchor="w")
        self.scrcpy_profile = tk.StringVar(value="low-latency")
        ttk.Label(actions, text="Profile", style="Subtle.TLabel").pack(anchor="w", pady=(8, 2))
        ttk.Combobox(actions, textvariable=self.scrcpy_profile, values=("manual", "low-latency", "recording"), state="readonly").pack(fill="x")
        self.scrcpy_audio = tk.BooleanVar(value=False)
        self.scrcpy_clipboard = tk.BooleanVar(value=False)
        self.scrcpy_stay_awake = tk.BooleanVar(value=False)
        ttk.Checkbutton(actions, text="Audio", variable=self.scrcpy_audio).pack(anchor="w", pady=(8, 0))
        ttk.Checkbutton(actions, text="Clipboard sync", variable=self.scrcpy_clipboard).pack(anchor="w")
        ttk.Checkbutton(actions, text="Keep device awake", variable=self.scrcpy_stay_awake).pack(anchor="w")
        ttk.Button(actions, text="Copy Serial", style="Secondary.TButton", command=self._copy_serial).pack(fill="x", pady=(8, 0))

        ttk.Label(right, text="Device Details", style="PanelTitle.TLabel").pack(anchor="w")
        self.detail_state = ttk.Label(right, text="Select a device", style="Info.TLabel")
        self.detail_state.pack(anchor="w", pady=(10, 14))
        self.detail_values: dict[str, ttk.Label] = {}
        detail_grid = ttk.Frame(right, style="Panel.TFrame")
        detail_grid.pack(fill="x")
        for row, label in enumerate(("Name", "Serial", "Model", "Product", "Android", "SDK", "Transport", "Last seen")):
            ttk.Label(detail_grid, text=label, style="Subtle.TLabel").grid(row=row, column=0, sticky="w", padx=(0, 20), pady=6)
            value = ttk.Label(detail_grid, text="-", style="Value.TLabel")
            value.grid(row=row, column=1, sticky="w", pady=6)
            self.detail_values[label] = value
        detail_grid.columnconfigure(1, weight=1)

        safety = ttk.LabelFrame(adb_tab, text="Safety", padding=12)
        safety.pack(fill="x", pady=(0, 12))
        safety.pack(fill="x", pady=(8, 8))
        self.dry_run_var = tk.BooleanVar(value=True)
        self.confirm_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(safety, text="Dry-run (safe default)", variable=self.dry_run_var, command=self._update_input_state).pack(side="left")
        ttk.Checkbutton(safety, text="Confirm input", variable=self.confirm_var, command=self._update_input_state).pack(side="left", padx=(14, 0))
        self.input_hint = ttk.Label(safety, text="Input blocked until confirmed", style="Warning.TLabel")
        self.input_hint.pack(side="right")

        capture_row = ttk.Frame(adb_tab, style="App.TFrame")
        capture_row.pack(fill="x", pady=(0, 7))
        ttk.Button(capture_row, text="Capture screenshot", style="Secondary.TButton", command=self._capture_screenshot).pack(side="left")
        ttk.Label(capture_row, text="Read-only", style="Subtle.TLabel").pack(side="left", padx=(10, 0), pady=(5, 0))

        shell_row = ttk.Frame(adb_tab, style="App.TFrame")
        shell_row.pack(fill="x", pady=(0, 7))
        ttk.Label(shell_row, text="shell", style="Subtle.TLabel").pack(side="left", padx=(0, 8))
        self.shell_entry = ttk.Entry(shell_row)
        self.shell_entry.pack(side="left", fill="x", expand=True)
        self.shell_entry.insert(0, "getprop ro.build.version.sdk")
        ttk.Button(shell_row, text="Run", style="Secondary.TButton", command=self._run_shell_gui).pack(side="left", padx=(8, 0))

        tap_row = ttk.Frame(adb_tab, style="App.TFrame")
        tap_row.pack(fill="x", pady=(0, 7))
        ttk.Label(tap_row, text="tap", style="Subtle.TLabel", width=6).pack(side="left")
        self.tap_x = self._entry(tap_row, "X", 5)
        self.tap_y = self._entry(tap_row, "Y", 5)
        self.tap_button = ttk.Button(tap_row, text="Send", style="Secondary.TButton", command=self._tap_gui)
        self.tap_button.pack(side="left", padx=(8, 0))

        swipe_row = ttk.Frame(adb_tab, style="App.TFrame")
        swipe_row.pack(fill="x", pady=(0, 7))
        ttk.Label(swipe_row, text="swipe", style="Subtle.TLabel", width=6).pack(side="left")
        self.swipe_entries = [self._entry(swipe_row, label, 4) for label in ("X1", "Y1", "X2", "Y2", "ms")]
        for entry in self.swipe_entries:
            entry.pack_configure(padx=(0, 4))
        self.swipe_entries[-1].insert(0, "300")
        self.swipe_button = ttk.Button(swipe_row, text="Send", style="Secondary.TButton", command=self._swipe_gui)
        self.swipe_button.pack(side="left")

        key_row = ttk.Frame(adb_tab, style="App.TFrame")
        key_row.pack(fill="x", pady=(0, 7))
        ttk.Label(key_row, text="keyevent", style="Subtle.TLabel", width=8).pack(side="left")
        self.key_entry = ttk.Entry(key_row, width=18)
        self.key_entry.pack(side="left")
        self.key_entry.insert(0, "KEYCODE_BACK")
        self.key_button = ttk.Button(key_row, text="Send", style="Secondary.TButton", command=self._keyevent_gui)
        self.key_button.pack(side="left", padx=(8, 0))

        text_row = ttk.Frame(adb_tab, style="App.TFrame")
        text_row.pack(fill="x")
        ttk.Label(text_row, text="text", style="Subtle.TLabel", width=8).pack(side="left")
        self.text_entry = ttk.Entry(text_row)
        self.text_entry.pack(side="left", fill="x", expand=True)
        self.text_button = ttk.Button(text_row, text="Send", style="Secondary.TButton", command=self._text_gui)
        self.text_button.pack(side="left", padx=(8, 0))
        workflow_root = app_directory() / "workflows" if getattr(sys, "frozen", False) else app_directory() / "adb_scrcpy" / "workflows"
        self.workflow_path = tk.StringVar(value=str(workflow_root / "device_smoke.json"))
        automation_header = ttk.Frame(workflow_tab, style="App.TFrame")
        automation_header.pack(fill="x")
        ttk.Label(automation_header, text="Automation", style="Header.TLabel").pack(side="left")
        self.automation_scope = ttk.Label(automation_header, text="Single-device run", style="Info.TLabel")
        self.automation_scope.pack(side="left", padx=(12, 0), pady=(5, 0))
        ttk.Button(automation_header, text="Open Editor", style="Secondary.TButton", command=lambda: self.tabs.select(editor_tab)).pack(side="right")
        ttk.Label(workflow_tab, text="Workflow JSON", style="PanelTitle.TLabel").pack(anchor="w", pady=(14, 0))
        workflow_entry = ttk.Entry(workflow_tab, textvariable=self.workflow_path)
        workflow_entry.pack(fill="x", pady=(10, 8))
        package_row = ttk.Frame(workflow_tab, style="App.TFrame")
        package_row.pack(fill="x", pady=(0, 8))
        ttk.Label(package_row, text="Target app package (optional)", style="Subtle.TLabel").pack(side="left")
        self.workflow_package_var = tk.StringVar(value="")
        package_entry = ttk.Entry(package_row, textvariable=self.workflow_package_var)
        package_entry.pack(side="left", fill="x", expand=True, padx=(10, 0))
        ttk.Label(package_row, text="Blank = no app restriction", style="Subtle.TLabel").pack(side="left", padx=(10, 0))
        repeat_row = ttk.Frame(workflow_tab, style="App.TFrame")
        repeat_row.pack(fill="x", pady=(0, 8))
        ttk.Label(repeat_row, text="Repeat workflow", style="Subtle.TLabel").pack(side="left")
        self.workflow_repeat_var = tk.IntVar(value=1)
        ttk.Spinbox(repeat_row, from_=1, to=999, textvariable=self.workflow_repeat_var, width=8).pack(side="left", padx=(10, 0))
        ttk.Label(repeat_row, text="time(s); default 1", style="Subtle.TLabel").pack(side="left", padx=(8, 0))
        workflow_actions = ttk.Frame(workflow_tab, style="App.TFrame")
        workflow_actions.pack(fill="x")
        ttk.Button(workflow_actions, text="Browse", style="Secondary.TButton", command=self._browse_workflow).pack(side="left")
        self.run_workflow_button = ttk.Button(workflow_actions, text="Run", style="Primary.TButton", command=self._run_workflow_gui)
        self.run_workflow_button.pack(side="left", padx=(6, 0))
        self.stop_workflow_button = ttk.Button(workflow_actions, text="Stop", style="Secondary.TButton", command=self._stop_workflow_gui, state="disabled")
        self.stop_workflow_button.pack(side="left", padx=(6, 0))
        self.pause_workflow_button = ttk.Button(workflow_actions, text="Pause", style="Secondary.TButton", command=self._toggle_workflow_pause, state="disabled")
        self.pause_workflow_button.pack(side="left", padx=(6, 0))
        self.workflow_hint = ttk.Label(workflow_tab, text="Simulation mode: no input sent", style="Info.TLabel")
        self.workflow_hint.pack(fill="x", pady=(10, 6))
        self.workflow_mode = ttk.Label(workflow_tab, text="SIMULATION - no input will be sent", style="Info.TLabel")
        self.workflow_mode.pack(anchor="w", pady=(0, 6))
        automation_safety = ttk.LabelFrame(workflow_tab, text="Automation safety", padding=8)
        automation_safety.pack(fill="x", pady=(0, 6))
        ttk.Checkbutton(automation_safety, text="Dry-run (no device changes)", variable=self.dry_run_var, command=self._update_input_state).pack(side="left")
        ttk.Checkbutton(automation_safety, text="Confirm input", variable=self.confirm_var, command=self._update_input_state).pack(side="left", padx=(14, 0))
        self.automation_input_hint = ttk.Label(automation_safety, text="Live input blocked", style="Warning.TLabel")
        self.automation_input_hint.pack(side="right")
        self.launch_app_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(workflow_tab, text="Launch target app before replay", variable=self.launch_app_var).pack(anchor="w")
        ttk.Label(workflow_tab, text="Live replay requires Dry-run off + Confirm input on.", style="Subtle.TLabel").pack(anchor="w", pady=(20, 0))
        progress_summary = ttk.Frame(workflow_tab, style="App.TFrame")
        progress_summary.pack(fill="x", pady=(20, 6))
        self.workflow_current = ttk.Label(progress_summary, text="No workflow running", style="PanelTitle.TLabel")
        self.workflow_current.pack(side="left")
        self.workflow_counts = ttk.Label(progress_summary, text="0/0 steps", style="Subtle.TLabel")
        self.workflow_counts.pack(side="right")
        self.workflow_progress = ttk.Progressbar(workflow_tab, mode="determinate", maximum=1, value=0)
        self.workflow_progress.pack(fill="x", pady=(0, 8))
        self.workflow_steps = ttk.Treeview(workflow_tab, columns=("kind", "status", "attempts", "elapsed", "error"), show="headings", height=3)
        for column, heading, width in (("kind", "Kind", 90), ("status", "Status", 100), ("attempts", "Attempts", 70), ("elapsed", "Elapsed", 80), ("error", "Error", 360)):
            self.workflow_steps.heading(column, text=heading)
            self.workflow_steps.column(column, width=width, stretch=column == "error")
        self.workflow_steps.pack(fill="x", expand=False)
        self.workflow_steps.tag_configure("running", background="#dceeff", foreground="#175ea8")
        self.workflow_steps.tag_configure("passed", background="#dff4e7", foreground="#166534")
        self.workflow_steps.tag_configure("failed", background="#fde2e1", foreground="#b42318")
        self.workflow_steps.tag_configure("stopped", background="#fff2cc", foreground="#8a5a00")
        ttk.Separator(workflow_tab, orient="horizontal").pack(fill="x", pady=(8, 6))
        queue_header = ttk.Frame(workflow_tab, style="App.TFrame")
        queue_header.pack(fill="x")
        ttk.Label(queue_header, text="Workflow Queue", style="PanelTitle.TLabel").pack(side="left")
        self.queue_summary = ttk.Label(queue_header, text="No queued devices", style="Info.TLabel")
        self.queue_summary.pack(side="left", padx=(10, 0))
        self.enqueue_button = ttk.Button(queue_header, text="Enqueue selected", style="Primary.TButton", command=self._enqueue_selected_workflow)
        self.enqueue_button.pack(side="right")
        queue_actions = ttk.Frame(workflow_tab, style="App.TFrame")
        queue_actions.pack(fill="x", pady=(8, 6))
        self.queue_pause_button = ttk.Button(queue_actions, text="Pause queue", style="Secondary.TButton", command=self._toggle_queue_pause, state="disabled")
        self.queue_pause_button.pack(side="left")
        self.queue_stop_button = ttk.Button(queue_actions, text="Stop queue", style="Secondary.TButton", command=self._stop_queue, state="disabled")
        self.queue_stop_button.pack(side="left", padx=(6, 0))
        self.queue_tree = ttk.Treeview(workflow_tab, columns=("serial", "status", "report", "error"), show="headings", height=2)
        for column, heading, width in (("serial", "Serial", 150), ("status", "Status", 100), ("report", "Report", 360), ("error", "Error", 280)):
            self.queue_tree.heading(column, text=heading)
            self.queue_tree.column(column, width=width, stretch=column in {"report", "error"})
        self.queue_tree.pack(fill="x")
        for tag, background, foreground in (("queued", "#eaf0f5", "#324152"), ("running", "#dceeff", "#175ea8"), ("passed", "#dff4e7", "#166534"), ("failed", "#fde2e1", "#b42318"), ("stopped", "#fff2cc", "#8a5a00")):
            self.queue_tree.tag_configure(tag, background=background, foreground=foreground)
        self.queue_tree.bind("<Double-1>", self._focus_queue_device)
        self._update_input_state()

        self.editor_spec: dict[str, object] | None = None
        self.editor_steps_data: list[dict[str, object]] = []
        self.editor_selected_index: int | None = None
        editor_top = ttk.Frame(editor_tab, style="App.TFrame")
        editor_top.pack(fill="x")
        ttk.Label(editor_top, text="Automation / Editor", style="Header.TLabel").pack(side="left")
        ttk.Button(editor_top, text="Back to Automation", style="Secondary.TButton", command=lambda: self.tabs.select(workflow_tab)).pack(side="right")
        ttk.Button(editor_top, text="Load current JSON", style="Secondary.TButton", command=self._load_editor_workflow).pack(side="right")
        self.editor_tree = ttk.Treeview(editor_tab, columns=("name", "kind", "summary"), show="headings", selectmode="browse", height=8)
        for column, heading, width in (("name", "Step", 230), ("kind", "Kind", 100), ("summary", "Action / Condition", 500)):
            self.editor_tree.heading(column, text=heading)
            self.editor_tree.column(column, width=width, stretch=column == "summary")
        self.editor_tree.pack(fill="both", expand=True, pady=(10, 10))
        self.editor_tree.bind("<<TreeviewSelect>>", self._editor_select)
        editor_form = ttk.LabelFrame(editor_tab, text="Selected step", padding=12)
        editor_form.pack(fill="x")
        form_row = ttk.Frame(editor_form, style="App.TFrame")
        form_row.pack(fill="x")
        ttk.Label(form_row, text="Name").pack(side="left")
        self.editor_name = ttk.Entry(form_row, width=25)
        self.editor_name.pack(side="left", padx=(6, 14))
        ttk.Label(form_row, text="Kind").pack(side="left")
        self.editor_kind = ttk.Combobox(form_row, values=("action", "wait", "assert", "screenshot", "stop"), state="readonly", width=12)
        self.editor_kind.pack(side="left", padx=(6, 14))
        ttk.Label(form_row, text="Timeout").pack(side="left")
        self.editor_timeout = ttk.Entry(form_row, width=8)
        self.editor_timeout.pack(side="left", padx=(6, 14))
        ttk.Label(form_row, text="Retries").pack(side="left")
        self.editor_retries = ttk.Entry(form_row, width=6)
        self.editor_retries.pack(side="left", padx=(6, 0))
        ttk.Label(editor_form, text="Payload JSON (action/condition/screenshot)").pack(anchor="w", pady=(10, 4))
        self.editor_payload = tk.Text(editor_form, height=5, wrap="word", font=("Cascadia Mono", 9), relief="solid", borderwidth=1)
        self.editor_payload.pack(fill="x")
        editor_buttons = ttk.Frame(editor_tab, style="App.TFrame")
        editor_buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(editor_buttons, text="Add step", style="Secondary.TButton", command=self._editor_add).pack(side="left")
        ttk.Button(editor_buttons, text="Update step", style="Secondary.TButton", command=self._editor_update).pack(side="left", padx=(6, 0))
        ttk.Button(editor_buttons, text="Delete step", style="Secondary.TButton", command=self._editor_delete).pack(side="left", padx=(6, 0))
        ttk.Button(editor_buttons, text="Move up", style="Secondary.TButton", command=lambda: self._editor_move(-1)).pack(side="left", padx=(20, 0))
        ttk.Button(editor_buttons, text="Move down", style="Secondary.TButton", command=lambda: self._editor_move(1)).pack(side="left", padx=(6, 0))
        ttk.Button(editor_buttons, text="Save JSON", style="Primary.TButton", command=self._editor_save).pack(side="right")
        self._load_editor_workflow(silent=True)

        ttk.Label(recorder_tab, text="Mouse Recorder", style="PanelTitle.TLabel").pack(anchor="w")
        self.recorder_state = ttk.Label(recorder_tab, text="Open scrcpy to record", style="Info.TLabel")
        self.recorder_state.pack(fill="x", pady=(10, 12))
        self.recorder_scope = ttk.Label(recorder_tab, text="Target: -  |  Geometry: -  |  0 events", style="Subtle.TLabel")
        self.recorder_scope.pack(anchor="w", pady=(0, 8))
        recorder_actions = ttk.Frame(recorder_tab, style="App.TFrame")
        recorder_actions.pack(fill="x")
        self.record_button = ttk.Button(recorder_actions, text="Record", style="Primary.TButton", command=self._start_recording, state="disabled")
        self.record_button.pack(side="left")
        self.save_recording_button = ttk.Button(recorder_actions, text="Stop & Save", style="Secondary.TButton", command=self._stop_recording, state="disabled")
        self.save_recording_button.pack(side="left", padx=(8, 0))
        recorder_text = ttk.Frame(recorder_tab, style="App.TFrame")
        recorder_text.pack(fill="x", pady=(12, 0))
        self.record_text_entry = ttk.Entry(recorder_text)
        self.record_text_entry.pack(side="left", fill="x", expand=True)
        self.add_record_text_button = ttk.Button(recorder_text, text="Add text", style="Secondary.TButton", command=self._add_recorded_text, state="disabled")
        self.add_record_text_button.pack(side="left", padx=(8, 0))
        ttk.Label(recorder_tab, text="Only mouse events in the foreground scrcpy window are captured.", style="Subtle.TLabel").pack(anchor="w", pady=(20, 0))
        ttk.Separator(recorder_tab, orient="horizontal").pack(fill="x", pady=(18, 12))
        ttk.Label(recorder_tab, text="Recorded Timeline", style="PanelTitle.TLabel").pack(anchor="w")
        self.recorder_events = ttk.Treeview(recorder_tab, columns=("kind", "target", "details"), show="headings", selectmode="browse", height=8)
        for column, heading, width in (("kind", "Kind", 100), ("target", "Step", 220), ("details", "Details", 500)):
            self.recorder_events.heading(column, text=heading)
            self.recorder_events.column(column, width=width, stretch=column == "details")
        self.recorder_events.pack(fill="both", expand=True, pady=(8, 8))
        self.recorder_events.bind("<<TreeviewSelect>>", self._recorder_select)
        recorder_edit = ttk.Frame(recorder_tab, style="App.TFrame")
        recorder_edit.pack(fill="x")
        self.recorder_event_payload = tk.Text(recorder_edit, height=4, wrap="word", font=("Cascadia Mono", 9), relief="solid", borderwidth=1)
        self.recorder_event_payload.pack(side="left", fill="both", expand=True)
        recorder_edit_buttons = ttk.Frame(recorder_edit, style="App.TFrame")
        recorder_edit_buttons.pack(side="left", padx=(8, 0))
        ttk.Button(recorder_edit_buttons, text="Update event", style="Secondary.TButton", command=self._recorder_update_event).pack(fill="x")
        ttk.Button(recorder_edit_buttons, text="Delete event", style="Secondary.TButton", command=self._recorder_delete_event).pack(fill="x", pady=(5, 0))
        ttk.Button(recorder_edit_buttons, text="Move up", style="Secondary.TButton", command=lambda: self._recorder_move(-1)).pack(fill="x", pady=(5, 0))
        ttk.Button(recorder_edit_buttons, text="Move down", style="Secondary.TButton", command=lambda: self._recorder_move(1)).pack(fill="x", pady=(5, 0))
        ttk.Button(recorder_edit_buttons, text="Add checkpoint", style="Secondary.TButton", command=self._recorder_checkpoint).pack(fill="x", pady=(5, 0))

        log_header = ttk.Frame(logs_tab, style="App.TFrame")
        log_header.pack(fill="x")
        ttk.Label(log_header, text="Activity Log", style="PanelTitle.TLabel").pack(side="left")
        self._latest_log_artifact: Path | None = None
        self.open_log_artifact_button = ttk.Button(log_header, text="Open latest artifact", style="Secondary.TButton", command=self._open_latest_log_artifact, state="disabled")
        self.open_log_artifact_button.pack(side="right", padx=(8, 0))
        self.log_severity_var = tk.StringVar(value="All")
        self.log_device_var = tk.StringVar(value="All")
        ttk.Combobox(log_header, textvariable=self.log_severity_var, values=("All", "Error", "Warning", "Info"), state="readonly", width=10).pack(side="right")
        self.log_device_filter = ttk.Combobox(log_header, textvariable=self.log_device_var, values=("All",), state="readonly", width=18)
        self.log_device_filter.pack(side="right", padx=(0, 8))
        self.log_severity_var.trace_add("write", lambda *_args: self._refresh_logs())
        self.log_device_var.trace_add("write", lambda *_args: self._refresh_logs())
        self.activity = tk.Text(logs_tab, height=20, wrap="word", state="disabled", bg="#101923", fg="#d8e3ef", insertbackground="#d8e3ef", relief="flat", font=("Cascadia Mono", 9), padx=10, pady=10)
        self.activity.pack(fill="both", expand=True, pady=(10, 0))
        self._log_entries: list[tuple[str, str, str]] = []

        ttk.Label(settings_tab, text="Settings", style="Header.TLabel").pack(anchor="w")
        ttk.Label(settings_tab, text="Preferences are local. Live confirmation and emergency state are never persisted.", style="Subtle.TLabel").pack(anchor="w", pady=(4, 16))
        appearance = ttk.LabelFrame(settings_tab, text="Appearance", padding=12)
        appearance.pack(fill="x")
        self.theme_var = tk.StringVar(value="Light")
        self.theme_var.trace_add("write", lambda *_args: self._apply_theme())
        ttk.Label(appearance, text="Theme").grid(row=0, column=0, sticky="w")
        ttk.Combobox(appearance, textvariable=self.theme_var, values=("Light", "Dark"), state="readonly", width=12).grid(row=0, column=1, sticky="w", padx=(10, 20))
        self.accent_var = tk.StringVar(value="#175ea8")
        self.accent_var.trace_add("write", lambda *_args: self._apply_theme())
        ttk.Label(appearance, text="Accent").grid(row=0, column=2, sticky="w")
        ttk.Entry(appearance, textvariable=self.accent_var, width=12).grid(row=0, column=3, sticky="w", padx=(10, 0))
        operations = ttk.LabelFrame(settings_tab, text="Operations", padding=12)
        operations.pack(fill="x", pady=(12, 0))
        self.poll_seconds_var = tk.IntVar(value=max(2, POLL_MS // 1000))
        self.retention_days_var = tk.IntVar(value=self.config.retention_days)
        self.default_profile_var = tk.StringVar(value=self.scrcpy_profile.get())
        ttk.Label(operations, text="Device poll seconds").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Spinbox(operations, from_=1, to=90, textvariable=self.poll_seconds_var, width=8).grid(row=0, column=1, sticky="w", padx=(12, 0), pady=5)
        ttk.Label(operations, text="Artifact retention days").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Spinbox(operations, from_=1, to=365, textvariable=self.retention_days_var, width=8).grid(row=1, column=1, sticky="w", padx=(12, 0), pady=5)
        ttk.Label(operations, text="Default scrcpy profile").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Combobox(operations, textvariable=self.default_profile_var, values=("manual", "low-latency", "recording"), state="readonly", width=18).grid(row=2, column=1, sticky="w", padx=(12, 0), pady=5)
        settings_actions = ttk.Frame(settings_tab)
        settings_actions.pack(fill="x", pady=(14, 0))
        self.save_preferences_button = ttk.Button(settings_actions, text="Save preferences", style="Primary.TButton", command=self._save_preferences)
        self.save_preferences_button.pack(side="left")
        ttk.Button(settings_actions, text="Open artifacts", command=self._open_artifacts).pack(side="left", padx=(8, 0))
        self.diagnostics_button = ttk.Button(settings_actions, text="Run diagnostics", command=self._run_diagnostics)
        self.diagnostics_button.pack(side="left", padx=(8, 0))
        self.diagnostics_output = tk.Text(settings_tab, height=10, wrap="word", state="disabled", bg="#101923", fg="#d8e3ef", relief="flat", font=("Cascadia Mono", 9), padx=10, pady=10)
        self.diagnostics_output.pack(fill="both", expand=True, pady=(14, 0))

        ttk.Label(files_tab, text="Files & APK", style="Header.TLabel").pack(anchor="w")
        self.files_scope = ttk.Label(files_tab, text="Device: select a ready device", style="Warning.TLabel")
        self.files_scope.pack(anchor="w", pady=(4, 6))
        ttk.Label(files_tab, text="Transfers and installation apply only to the selected device.", style="Subtle.TLabel").pack(anchor="w", pady=(0, 16))
        self.file_progress = ttk.Progressbar(files_tab, mode="indeterminate")
        self.file_progress.pack(fill="x", pady=(0, 12))
        self.file_status = ttk.Label(files_tab, text="Ready", style="Info.TLabel")
        self.file_status.pack(fill="x", pady=(0, 10))
        file_actions = ttk.Notebook(files_tab)
        file_actions.pack(fill="both", expand=True)
        push_frame = ttk.LabelFrame(file_actions, text="Push file to device", padding=12)
        self.push_source = tk.StringVar()
        self.push_destination = tk.StringVar(value="/sdcard/Download/")
        push_row = ttk.Frame(push_frame)
        push_row.pack(fill="x")
        ttk.Entry(push_row, textvariable=self.push_source).pack(side="left", fill="x", expand=True)
        ttk.Button(push_row, text="Browse", command=self._browse_push).pack(side="left", padx=(8, 0))
        ttk.Label(push_frame, text="Android destination").pack(anchor="w", pady=(8, 2))
        ttk.Entry(push_frame, textvariable=self.push_destination).pack(fill="x")
        ttk.Button(push_frame, text="Push", style="Primary.TButton", command=self._push_file).pack(anchor="e", pady=(8, 0))

        pull_frame = ttk.LabelFrame(file_actions, text="Pull file from device", padding=12)
        self.pull_source = tk.StringVar(value="/sdcard/Download/")
        self.pull_destination = tk.StringVar()
        self.pull_file_info = ttk.Label(pull_frame, text="No device file selected", style="Subtle.TLabel")
        self.pull_file_info.pack(anchor="w", pady=(0, 6))
        self.pull_preview_button = ttk.Button(pull_frame, text="Preview", command=self._preview_pull_file, state="disabled")
        self.pull_preview_button.pack(anchor="w", pady=(0, 8))
        ttk.Label(pull_frame, text="Android source").pack(anchor="w")
        ttk.Entry(pull_frame, textvariable=self.pull_source).pack(fill="x", pady=(2, 8))
        ttk.Button(pull_frame, text="Browse device files", command=self._browse_device_files).pack(anchor="w", pady=(0, 8))
        pull_row = ttk.Frame(pull_frame)
        pull_row.pack(fill="x")
        ttk.Entry(pull_row, textvariable=self.pull_destination).pack(side="left", fill="x", expand=True)
        ttk.Button(pull_row, text="Browse", command=self._browse_pull).pack(side="left", padx=(8, 0))
        ttk.Button(pull_frame, text="Pull", style="Primary.TButton", command=self._pull_file).pack(anchor="e", pady=(8, 0))

        apk_frame = ttk.LabelFrame(file_actions, text="Install APK", padding=12)
        self.apk_source = tk.StringVar()
        apk_row = ttk.Frame(apk_frame)
        apk_row.pack(fill="x")
        ttk.Entry(apk_row, textvariable=self.apk_source).pack(side="left", fill="x", expand=True)
        ttk.Button(apk_row, text="Browse APK", command=self._browse_apk).pack(side="left", padx=(8, 0))
        ttk.Button(apk_frame, text="Install", style="Primary.TButton", command=self._install_apk).pack(anchor="e", pady=(8, 0))
        file_actions.add(push_frame, text="Push")
        file_actions.add(pull_frame, text="Pull")
        file_actions.add(apk_frame, text="Install APK")
        self.file_actions = file_actions

        focus_header = ttk.Frame(focus_tab, style="App.TFrame")
        focus_header.pack(fill="x")
        ttk.Label(focus_header, text="Focus View", style="Header.TLabel").pack(side="left")
        self.focus_title = ttk.Label(focus_header, text="Select a device from Devices", style="Subtle.TLabel")
        self.focus_title.pack(side="left", padx=(12, 0), pady=(8, 0))
        ttk.Button(focus_header, text="Back to Devices", style="Secondary.TButton", command=lambda: self.tabs.select(devices_tab)).pack(side="right")
        focus_body = ttk.Panedwindow(focus_tab, orient="horizontal")
        focus_body.pack(fill="both", expand=True, pady=(14, 0))
        focus_media = ttk.Frame(focus_body, style="Panel.TFrame", padding=14)
        focus_info = ttk.Frame(focus_body, style="Panel.TFrame", padding=14)
        focus_body.add(focus_media, weight=5)
        focus_body.add(focus_info, weight=4)
        self.focus_image_label = tk.Label(focus_media, text="No device selected", bg="#101923", fg="#d8e3ef")
        self.focus_image_label.pack(fill="both", expand=True)
        ttk.Label(focus_media, text="Latest ADB screenshot / scrcpy opens in its own window", style="Subtle.TLabel").pack(anchor="w", pady=(8, 0))
        ttk.Label(focus_info, text="Device Health", style="PanelTitle.TLabel").pack(anchor="w")
        self.focus_health = ttk.Label(focus_info, text="Not checked", style="Info.TLabel")
        self.focus_health.pack(anchor="w", pady=(8, 12))
        self.focus_info_values: dict[str, ttk.Label] = {}
        focus_grid = ttk.Frame(focus_info, style="Panel.TFrame")
        focus_grid.pack(fill="x")
        for row, label in enumerate(("Name", "Serial", "Model", "Android", "SDK", "Geometry", "Foreground", "Last seen")):
            ttk.Label(focus_grid, text=label, style="Subtle.TLabel").grid(row=row, column=0, sticky="w", padx=(0, 16), pady=5)
            value = ttk.Label(focus_grid, text="-", style="Value.TLabel")
            value.grid(row=row, column=1, sticky="w", pady=5)
            self.focus_info_values[label] = value
        focus_grid.columnconfigure(1, weight=1)
        focus_actions = ttk.Frame(focus_info, style="Panel.TFrame")
        focus_actions.pack(fill="x", pady=(18, 0))
        self.focus_check_button = ttk.Button(focus_actions, text="Check health", style="Primary.TButton", command=self._check_selected, state="disabled")
        self.focus_check_button.pack(fill="x")
        self.focus_open_button = ttk.Button(focus_actions, text="Open scrcpy", style="Secondary.TButton", command=self._open_scrcpy, state="disabled")
        self.focus_open_button.pack(fill="x", pady=(7, 0))
        self.focus_stop_button = ttk.Button(focus_actions, text="Stop scrcpy", style="Secondary.TButton", command=self._stop_scrcpy, state="disabled")
        self.focus_stop_button.pack(fill="x", pady=(7, 0))
        ttk.Button(focus_actions, text="Capture screenshot", style="Secondary.TButton", command=self._capture_screenshot).pack(fill="x", pady=(7, 0))
        ttk.Label(focus_info, text="Read-only shell", style="PanelTitle.TLabel").pack(anchor="w", pady=(20, 6))
        focus_shell = ttk.Frame(focus_info, style="Panel.TFrame")
        focus_shell.pack(fill="x")
        self.focus_shell_entry = ttk.Entry(focus_shell)
        self.focus_shell_entry.pack(side="left", fill="x", expand=True)
        self.focus_shell_entry.insert(0, "getprop ro.build.version.release")
        ttk.Button(focus_shell, text="Run", style="Secondary.TButton", command=self._run_focus_shell).pack(side="left", padx=(7, 0))

        footer = ttk.Frame(root, style="App.TFrame")
        footer.pack(fill="x", pady=(12, 0))
        self.status = ttk.Label(footer, text="Ready", style="Subtle.TLabel")
        self.status.pack(side="left")
        ttk.Label(footer, text="Dry-run protects device input by default.", style="Subtle.TLabel").pack(side="right")
        self._update_selection_summary()
        self._add_tooltip(self.emergency_button, "Stop workflow, recorder and scrcpy sessions managed by this app")
        self._add_tooltip(self.game_safe_button, "Pause polling, scrcpy and the local ADB daemon for gameplay")
        self._add_tooltip(self.reset_safety_button, "Clear the latched emergency stop after checking the device")
        self._add_tooltip(self.open_log_artifact_button, "Open the latest existing report, screenshot or log path")

    @staticmethod
    def _add_tooltip(widget: tk.Misc, message: str) -> None:
        state: dict[str, tk.Toplevel | None] = {"window": None}
        def show(_event: object = None) -> None:
            if state["window"] is not None or str(widget["state"]) == "disabled":
                return
            tip = tk.Toplevel(widget)
            tip.wm_overrideredirect(True)
            tip.attributes("-topmost", True)
            tip.geometry(f"+{widget.winfo_rootx()}+{widget.winfo_rooty() + widget.winfo_height() + 4}")
            tk.Label(tip, text=message, background="#fff8dc", foreground="#3d3520", relief="solid", borderwidth=1, padx=8, pady=4, wraplength=360).pack()
            state["window"] = tip
        def hide(_event: object = None) -> None:
            tip = state["window"]
            if tip is not None:
                tip.destroy()
                state["window"] = None
        widget.bind("<Enter>", show, add="+")
        widget.bind("<Leave>", hide, add="+")

    @staticmethod
    def _entry(parent: ttk.Frame, placeholder: str, width: int) -> ttk.Entry:
        entry = ttk.Entry(parent, width=width)
        entry.pack(side="left", padx=(0, 5))
        return entry

    def _toggle_devices(self) -> None:
        self.devices_expanded = not self.devices_expanded
        if self.devices_expanded:
            self.device_list_frame.pack(fill="both", expand=True)
            self.device_toggle.configure(text="Collapse")
        else:
            self.device_list_frame.pack_forget()
            self.device_toggle.configure(text="Expand")

    def _on_grid_canvas_configure(self, event: object) -> None:
        width = max(1, int(getattr(event, "width", 1)))
        self.grid_canvas.itemconfigure(self.grid_window, width=width)
        if width != self._grid_canvas_width:
            self._grid_canvas_width = width
            self.after_idle(self._render_device_grid)

    def _select_route(self, route: str) -> None:
        target = self._sidebar_routes.get(route)
        if target is None:
            return
        self.tabs.select(target)
        self._sync_sidebar_route(route)

    def _sync_sidebar_route(self, route: str | None = None) -> None:
        if route is None and hasattr(self, "tabs"):
            selected = self.tabs.select()
            route = next((name for name, target in self._sidebar_routes.items() if name != "Dashboard" and str(target) == selected), None)
            if route is None:
                route = next((name for name, target in self._sidebar_routes.items() if str(target) == selected), "Devices")
        route = route or "Devices"
        for name, button in self._sidebar_buttons.items():
            button.configure(style="NavActive.TButton" if name == route else "Nav.TButton")

    def _on_route_changed(self, _event: object = None) -> None:
        self._sync_sidebar_route()

    def _ui_state_path(self) -> Path:
        return self.config.artifacts_dir / ".gui_state.json"

    def _device_metadata_path(self) -> Path:
        return self.config.artifacts_dir / "device_metadata.json"

    def _read_device_metadata(self) -> dict[str, dict[str, object]]:
        try:
            payload = json.loads(self._device_metadata_path().read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}

    def _write_device_metadata(self) -> None:
        path = self._device_metadata_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self._device_meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def _meta(self, serial: str) -> dict[str, object]:
        return self._device_meta.setdefault(serial, {"name": self.config.device_names.get(serial, ""), "group": "", "role": "", "location": "", "environment": "test", "tags": []})

    def _visible_devices(self) -> list[Device]:
        state_filter = self.filter_state_var.get()
        model_filter = self.filter_model_var.get()
        android_filter = self.filter_android_var.get()
        tag_filter = self.filter_tag_var.get().strip().lower()
        app_filter = self.filter_app_var.get().strip().lower()
        visible: list[Device] = []
        for device in self._known_devices.values():
            meta = self._meta(device.serial)
            tags = " ".join(str(tag) for tag in meta.get("tags", []))
            if state_filter != "All" and state_label(device.state) != state_filter:
                continue
            if model_filter != "All" and (device.model or "-") != model_filter:
                continue
            if android_filter != "All" and self._grid_android.get(device.serial, "-") != android_filter:
                continue
            if tag_filter and tag_filter not in tags.lower() and tag_filter not in str(meta.get("group", "")).lower() and tag_filter not in str(meta.get("role", "")).lower():
                continue
            if app_filter and app_filter not in self._grid_foreground.get(device.serial, "").lower():
                continue
            visible.append(device)
        return visible

    def _update_selection_summary(self) -> None:
        self.selection_summary.configure(text=f"{len(self._selected_serials)} selected")
        if hasattr(self, "automation_scope"):
            if len(self._selected_serials) > 1:
                self.automation_scope.configure(text=f"{len(self._selected_serials)}-device queue", style="Info.TLabel")
            elif self._current_serial:
                self.automation_scope.configure(text=f"Focus: {self._current_serial}", style="Info.TLabel")
            else:
                self.automation_scope.configure(text="Select a device", style="Warning.TLabel")

    def _set_selected(self, serial: str, selected: bool) -> None:
        if selected:
            self._selected_serials.add(serial)
        else:
            self._selected_serials.discard(serial)
        self._update_selection_summary()

    def _select_visible_devices(self) -> None:
        self._selected_serials.update(device.serial for device in self._visible_devices())
        self._update_selection_summary()
        self._render_device_grid()

    def _clear_selection(self) -> None:
        self._selected_serials.clear()
        self._update_selection_summary()
        self._render_device_grid()

    def _confirm_batch(self, title: str, action: str) -> list[str] | None:
        serials = sorted(self._selected_serials)
        if not serials:
            self._handle_error("Select at least one device")
            return None
        preview = "\n".join(serials[:16])
        if not messagebox.askyesno(title, f"{action} will affect {len(serials)} device(s):\n\n{preview}\n\nContinue?", parent=self):
            self._set_status("Batch action cancelled")
            return None
        return serials

    def _batch_health(self) -> None:
        serials = self._confirm_batch("Confirm batch health", "Health check")
        if serials is not None:
            self._run_batch(serials, "Batch health", lambda client: DeviceHealthMonitor(client, self.config.package).assess())

    def _batch_screenshot(self) -> None:
        serials = self._confirm_batch("Confirm batch screenshots", "Capture screenshot")
        if serials is None:
            return
        def capture(client: AdbClient):
            output = self._gui_session_dir(client.serial) / f"batch_{time.strftime('%H%M%S')}.png"
            return client.save_screenshot(output)
        self._run_batch(serials, "Batch screenshots", capture)

    def _run_batch(self, serials: list[str], label: str, operation: Callable[[AdbClient], object]) -> None:
        self._log(f"BATCH START  {label}  scope={','.join(serials)}")
        def run() -> list[tuple[str, str]]:
            results: list[tuple[str, str]] = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                futures = {executor.submit(operation, AdbClient(serial, self.config.adb_path, self.config.command_timeout)): serial for serial in serials}
                for future in concurrent.futures.as_completed(futures):
                    serial = futures[future]
                    try:
                        future.result()
                        results.append((serial, "passed"))
                    except Exception as exc:
                        results.append((serial, f"failed: {exc}"))
            return results
        def done(results: object) -> None:
            for serial, status in results:  # type: ignore[misc]
                self._log(f"BATCH  {serial}  {status}")
            self._set_status(f"{label} completed")
        self._submit(run, done, label)

    def _read_ui_state(self) -> dict[str, object]:
        try:
            payload = json.loads(self._ui_state_path().read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}

    def _write_ui_state(self) -> None:
        payload = {
            "window_geometry": self.geometry(),
            "tab_index": self.tabs.index(self.tabs.select()),
            "serial": self._current_serial,
            "workflow_path": self.workflow_path.get(),
            "dry_run": True,
        }
        try:
            path = self._ui_state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
        except OSError:
            self._log("STATE  unable to save UI preferences")

    def _open_artifacts(self) -> None:
        path = self.config.artifacts_dir
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(path))
        except AttributeError:
            self._set_status(f"Artifacts: {path}")
        except OSError as exc:
            self._handle_error(f"Cannot open artifacts: {exc}")
        else:
            self._set_status(f"Opened artifacts: {path}")

    def _queue(self, callback: Callable[[], None]) -> None:
        if not self._closing:
            self._events.put(callback)

    def _drain_events(self) -> None:
        if self._closing:
            return
        try:
            while True:
                self._events.get_nowait()()
        except queue.Empty:
            pass
        self.after(50, self._drain_events)

    def _submit(
        self,
        action: Callable[[], object],
        on_success: Callable[[object], None],
        label: str,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self._set_status(label)
        future = self._executor.submit(action)
        def completed(done: concurrent.futures.Future) -> None:
            try:
                value = done.result()
            except Exception as exc:
                message = str(exc)
                handler = on_error or self._handle_error
                self._queue(lambda: handler(message))
            else:
                self._queue(lambda: on_success(value))
        future.add_done_callback(completed)

    def _poll_devices(self) -> None:
        if self._game_safe_mode:
            return
        if self._poll_pending:
            return
        self._poll_pending = True
        def done(devices: object) -> None:
            self._poll_pending = False
            self._update_devices(list(devices))
        self._submit(self.device_manager.list_devices, done, "Scanning for USB devices...")
        self.after(POLL_MS, self._poll_devices)

    def _grid_profile(self, count: int) -> tuple[int, int, int]:
        if count <= 1:
            return 320, 712, 2_000
        if count <= 4:
            return 260, 578, 3_000
        if count <= 8:
            return 220, 489, 5_000
        return 180, 400, 10_000

    def _grid_columns(self, count: int) -> int:
        """Choose columns from available width while retaining a count fallback before mapping."""
        if count <= 0:
            return 1
        if self._grid_canvas_width > 1:
            return max(1, min(4, self._grid_canvas_width // 220))
        return 1 if count <= 1 else 2 if count <= 4 else 3 if count <= 8 else 4

    def _poll_grid_thumbnails(self) -> None:
        if self._game_safe_mode:
            return
        devices = self._visible_devices()
        if devices:
            _, _, interval = self._grid_profile(len(devices))
            for device in devices:
                if device.ready and device.serial not in self._grid_pending:
                    self._grid_pending.add(device.serial)
                    self._submit_grid_capture(device)
            self.after(interval, self._poll_grid_thumbnails)
        else:
            self.after(2_000, self._poll_grid_thumbnails)

    def _submit_grid_capture(self, device: Device) -> None:
        width, height, _ = self._grid_profile(len(self._known_devices))
        client = AdbClient(device.serial, adb_path=self.config.adb_path, command_timeout=self.config.command_timeout)
        def capture() -> tuple[str, bytes, str, str]:
            raw = client.screencap(retries=0)
            foreground = "unknown"
            android = "-"
            try:
                activity = client.shell("dumpsys", "activity", "activities", retries=0)
                import re
                match = re.search(r"(?:mResumedActivity|topResumedActivity)=.*?\s([A-Za-z0-9_.]+)/(?:[A-Za-z0-9_.$]+)", activity)
                if match is None:
                    match = re.search(r"\s([A-Za-z0-9_.]+)/(?:[A-Za-z0-9_.$]+)", activity)
                foreground = match.group(1) if match else "unknown"
            except Exception:
                pass
            try:
                android = client.shell("getprop", "ro.build.version.release", retries=0).strip() or "-"
            except Exception:
                pass
            return device.serial, raw, foreground, android
        def done(value: object) -> None:
            serial, raw, foreground, android = value  # type: ignore[misc]
            self._grid_pending.discard(serial)
            self._grid_foreground[serial] = foreground
            self._grid_android[serial] = android
            try:
                from PIL import Image, ImageTk
                image = Image.open(BytesIO(raw)).convert("RGB")
                image.thumbnail((width, height), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(image)
                self._grid_images[serial] = photo
                label = self._grid_thumbnail_labels.get(serial)
                if label:
                    label.configure(image=photo, text="")
            except Exception as exc:
                self._log(f"GRID THUMBNAIL ERROR  {serial}: {exc}")
            self._render_device_grid()
        def failed(message: str) -> None:
            self._grid_pending.discard(device.serial)
            self._log(f"GRID  {device.serial} thumbnail unavailable: {message}")
        self._submit(capture, done, f"Updating thumbnail {device.serial}", failed)

    def _render_device_grid(self) -> None:
        if not hasattr(self, "grid_inner"):
            return
        for child in self.grid_inner.winfo_children():
            child.destroy()
        self._grid_thumbnail_labels = {}
        devices = list(self._known_devices.values())
        count = len(devices)
        columns = self._grid_columns(count)
        for index, device in enumerate(devices):
            card = ttk.Frame(self.grid_inner, style="Panel.TFrame", padding=8)
            card.grid(row=index // columns, column=index % columns, sticky="nsew", padx=6, pady=6)
            self.grid_inner.columnconfigure(index % columns, weight=1)
            meta = self._meta(device.serial)
            name = str(meta.get("name") or self.config.device_names.get(device.serial, device.model or "Android device"))
            ttk.Label(card, text=name, style="PanelTitle.TLabel").pack(anchor="w")
            ttk.Label(card, text=state_label(device.state), style="Ready.TLabel" if device.ready else "Warning.TLabel").pack(anchor="w", pady=(4, 4))
            thumbnail = self._grid_images.get(device.serial)
            image_label = tk.Label(card, image=thumbnail, text="No thumbnail", width=180, height=120, bg="#eaf0f5", fg="#5c6875")
            image_label.pack(fill="x", pady=(2, 6))
            self._grid_thumbnail_labels[device.serial] = image_label
            last_seen = time.strftime("%H:%M:%S", time.localtime(self._last_seen.get(device.serial, time.time())))
            foreground = self._grid_foreground.get(device.serial, "unknown")
            tags = ", ".join(str(tag) for tag in meta.get("tags", [])) or "untagged"
            ttk.Label(card, text=f"{device.serial}\n{device.model or '-'}\nApp: {foreground}\nTags: {tags}\nSeen: {last_seen}", style="Subtle.TLabel", justify="left").pack(anchor="w")
            selected = tk.BooleanVar(value=device.serial in self._selected_serials)
            ttk.Checkbutton(card, text="Select", variable=selected, command=lambda serial=device.serial, var=selected: self._set_selected(serial, bool(var.get()))).pack(anchor="w", pady=(5, 0))
            for widget in (card, image_label):
                widget.bind("<Button-1>", lambda _event, serial=device.serial: self._focus_grid_device(serial))
        self.grid_inner.update_idletasks()
        self._update_selection_summary()

    def _focus_grid_device(self, serial: str) -> None:
        self._select_device(serial)
        if self.tree.exists(serial):
            self.tree.selection_set(serial)
        self._update_focus_view()
        self.tabs.select(self.focus_tab)
        self._set_status(f"Focused {serial}")

    def _focus_queue_device(self, _event: object = None) -> None:
        """Focus the device represented by the selected queue row."""
        selected = self.queue_tree.selection()
        if not selected:
            return
        serial = selected[0]
        if serial not in self._known_devices:
            self._set_status(f"Queue device {serial} is not currently connected")
            return
        self._focus_grid_device(serial)

    def _update_focus_view(self) -> None:
        serial = self._current_serial
        device = self._known_devices.get(serial or "")
        if not device:
            self.focus_title.configure(text="Select a device from Devices")
            self.focus_health.configure(text="Not checked", style="Info.TLabel")
            self.focus_image_label.configure(image="", text="No device selected")
            return
        name = self.config.device_names.get(device.serial, device.model or "Android device")
        self.focus_title.configure(text=f"{name}  |  {device.serial}")
        values = {
            "Name": name,
            "Serial": device.serial,
            "Model": device.model or "-",
            "Android": self.detail_values["Android"].cget("text") if "Android" in self.detail_values else "-",
            "SDK": self.detail_values["SDK"].cget("text") if "SDK" in self.detail_values else "-",
            "Geometry": "-",
            "Foreground": self._grid_foreground.get(device.serial, "unknown"),
            "Last seen": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._last_seen.get(device.serial, time.time()))),
        }
        for label, value in values.items():
            self.focus_info_values[label].configure(text=value)
        image = self._grid_images.get(device.serial)
        if image:
            self.focus_image_label.configure(image=image, text="")
        self.focus_open_button.configure(state="normal" if device.ready and self.scrcpy_manager.status(device.serial) != "running" else "disabled")
        self.focus_stop_button.configure(state="normal" if self.scrcpy_manager.status(device.serial) == "running" else "disabled")
        self.focus_check_button.configure(state="normal" if device.ready else "disabled")

    def _run_focus_shell(self) -> None:
        if not self._current_serial:
            return
        self.shell_entry.delete(0, "end")
        self.shell_entry.insert(0, self.focus_shell_entry.get())
        self._run_shell_gui()

    def _poll_scrcpy_sessions(self) -> None:
        if self._game_safe_mode:
            return
        active = {session.serial: session for session in self.scrcpy_manager.sessions()}
        disappeared = self._active_scrcpy_serials - set(active)
        for serial in disappeared:
            self._log(f"SCRCPY EXIT  {serial}  process ended")
            if serial == self._current_serial:
                self._set_status("scrcpy process ended")
        self._active_scrcpy_serials = set(active)
        if self._current_serial:
            session = active.get(self._current_serial)
            if session:
                self.stop_button.configure(state="normal")
                self.open_button.configure(state="disabled")
                if self._recorder is None:
                    self.record_button.configure(state="normal")
                    self.quick_record_button.configure(state="normal", text="Record", command=self._start_recording)
                else:
                    self.quick_record_button.configure(state="normal", text="Stop & Save", command=self._stop_recording)
            elif self._current_serial not in self._opening_scrcpy:
                device = self._known_devices.get(self._current_serial)
                self.stop_button.configure(state="disabled")
                self.open_button.configure(state="normal" if device and device.ready else "disabled")
                self.record_button.configure(state="disabled")
                self.quick_record_button.configure(state="disabled", text="Record", command=self._start_recording)
        self.after(SCRCPY_POLL_MS, self._poll_scrcpy_sessions)

    def _apply_game_safe_mode_ui(self) -> None:
        """Keep all device-changing controls visibly unavailable while paused."""
        if not hasattr(self, "game_safe_button"):
            return
        self.game_safe_button.configure(
            text="Resume controller" if self._game_safe_mode else "Game safe mode",
            command=self._resume_game_safe_mode if self._game_safe_mode else self._enter_game_safe_mode,
            state="normal",
        )
        blocked = (
            "check_button", "open_button", "stop_button", "reconnect_button", "metadata_button",
            "quick_record_button", "record_button", "save_recording_button", "add_record_text_button",
            "tap_button", "swipe_button", "key_button", "text_button", "run_workflow_button",
            "stop_workflow_button", "pause_workflow_button", "enqueue_button", "queue_pause_button",
            "queue_stop_button", "focus_check_button", "focus_open_button", "focus_stop_button",
            "diagnostics_button", "pull_preview_button",
        )
        for name in blocked:
            widget = getattr(self, name, None)
            if widget is not None:
                widget.configure(state="disabled" if self._game_safe_mode else "normal")
        if self._game_safe_mode:
            self.summary_state.configure(text="Controller paused")
            self.detail_state.configure(text="Game Safe Mode: ADB paused", style="Warning.TLabel")
        self._update_input_state()

    def _enter_game_safe_mode(self) -> None:
        if self._game_safe_mode:
            return
        if not messagebox.askyesno(
            "Enter Game Safe Mode",
            "Stop workflows, close scrcpy and stop the local ADB daemon?\n\n"
            "This does not change Android developer settings or bypass app protections.",
            parent=self,
        ):
            return
        self._game_safe_mode = True
        if self._workflow_context is not None:
            self._workflow_context.stop_requested = True
            self._workflow_context.pause_requested = False
        if self._workflow_queue is not None:
            self._workflow_queue.stop()
        self._stop_recording(save=False)
        self.scrcpy_manager.stop_all()
        self._active_scrcpy_serials.clear()
        self._poll_pending = False
        self._grid_pending.clear()
        self._apply_game_safe_mode_ui()
        self._set_status("Pausing controller and ADB...")

        def done(_result: object) -> None:
            self._log("GAME SAFE MODE  controller paused; local ADB daemon stopped")
            self._set_status("Game Safe Mode active; press Resume controller when finished")

        def failed(message: str) -> None:
            self._log(f"WARNING  Game Safe Mode active, ADB stop reported: {message}")
            self._set_status("Game Safe Mode active; ADB stop needs attention")

        self._submit(self.device_manager.stop_server, done, "Stopping local ADB daemon...", failed)

    def _resume_game_safe_mode(self) -> None:
        if not self._game_safe_mode:
            return
        if not messagebox.askyesno(
            "Resume controller",
            "Start the local ADB daemon and resume device polling?",
            parent=self,
        ):
            return
        self.game_safe_button.configure(state="disabled")

        def done(_result: object) -> None:
            self._game_safe_mode = False
            self._apply_game_safe_mode_ui()
            self._log("GAME SAFE MODE  controller resumed; local ADB daemon restarted")
            self._set_status("Controller resumed; scanning for USB devices...")
            self.after(0, self._poll_devices)
            self.after(0, self._poll_scrcpy_sessions)
            self.after(0, self._poll_grid_thumbnails)

        def failed(message: str) -> None:
            self.game_safe_button.configure(state="normal")
            self._log(f"ERROR  Cannot resume controller: {message}")
            self._set_status("Resume failed; controller remains paused")

        self._submit(self.device_manager.reconnect, done, "Starting local ADB daemon...", failed)

    def _update_devices(self, devices: list[Device]) -> None:
        selected = self._current_serial
        if selected is None:
            saved_serial = self._saved_ui_state.get("serial")
            if isinstance(saved_serial, str):
                selected = saved_serial
        now = time.time()
        for device in devices:
            self._last_seen[device.serial] = now
        self._known_devices = {device.serial: device for device in devices}
        models = sorted({device.model or "-" for device in devices})
        self.filter_model.configure(values=("All", *models))
        if self.filter_model_var.get() not in {"All", *models}:
            self.filter_model_var.set("All")
        android_versions = sorted({self._grid_android.get(device.serial, "-") for device in devices})
        self.filter_android.configure(values=("All", *android_versions))
        if self.filter_android_var.get() not in {"All", *android_versions}:
            self.filter_android_var.set("All")
        for item in self.tree.get_children():
            self.tree.delete(item)
        for device in devices:
            name = str(self._meta(device.serial).get("name") or self.config.device_names.get(device.serial, device.model or "Android device"))
            last_seen = time.strftime("%H:%M:%S", time.localtime(self._last_seen[device.serial]))
            self.tree.insert("", "end", iid=device.serial, text=device.serial, values=(name, state_label(device.state), device.model or "-", last_seen))
        self._render_device_grid()
        if selected and selected in self._known_devices:
            self.tree.selection_set(selected)
        elif devices:
            self.tree.selection_set(devices[0].serial)
            self._select_device(devices[0].serial)
        else:
            self._select_device(None)
        ready_count = sum(device.ready for device in devices)
        self.summary_count.configure(text=f"{len(devices)} device{'s' if len(devices) != 1 else ''}")
        self.summary_state.configure(text=f"{ready_count} ready")
        self._set_status("Device list updated")

    def _on_select(self, _event: object) -> None:
        selected = self.tree.selection()
        self._select_device(selected[0] if selected else None)

    def _select_device(self, serial: str | None) -> None:
        self._current_serial = serial
        self._update_selection_summary()
        device = self._known_devices.get(serial or "")
        if not device:
            if hasattr(self, "files_scope"):
                self.files_scope.configure(text="Device: select a ready device", style="Warning.TLabel")
            self.detail_state.configure(text="Select a device", style="Info.TLabel")
            for value in self.detail_values.values():
                value.configure(text="-")
            self.check_button.configure(state="disabled")
            self.open_button.configure(state="disabled")
            self.stop_button.configure(state="disabled")
            self.reconnect_button.configure(state="normal")
            self.metadata_button.configure(state="disabled")
            self.quick_record_button.configure(state="disabled", text="Record", command=self._start_recording)
            self._update_focus_view()
            return
        style = "Ready.TLabel" if device.ready else "Warning.TLabel"
        if hasattr(self, "files_scope"):
            self.files_scope.configure(
                text=f"Device: {device.serial}  |  {state_label(device.state)}",
                style="Ready.TLabel" if device.ready else "Warning.TLabel",
            )
        self.detail_state.configure(text=state_label(device.state), style=style)
        last_seen = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._last_seen.get(device.serial, time.time())))
        data = {"Name": self.config.device_names.get(device.serial, device.model or "Android device"), "Serial": device.serial, "Model": device.model or "-", "Product": device.product or "-", "Android": "-", "SDK": "-", "Transport": device.transport_id or "-", "Last seen": last_seen}
        for label, value in data.items():
            self.detail_values[label].configure(text=value)
        state = "normal" if device.ready else "disabled"
        self.check_button.configure(state=state)
        running = self.scrcpy_manager.status(device.serial) == "running"
        opening = device.serial in self._opening_scrcpy
        self.open_button.configure(state="disabled" if running or opening else state)
        self.stop_button.configure(state="normal" if running else "disabled")
        self.record_button.configure(state="normal" if running and self._recorder is None else "disabled")
        self.quick_record_button.configure(state="normal" if running else "disabled", text="Stop & Save" if self._recorder else "Record", command=self._stop_recording if self._recorder else self._start_recording)
        self.reconnect_button.configure(state="normal")
        self.metadata_button.configure(state="normal")
        self._update_focus_view()

    def _check_selected(self) -> None:
        serial = self._current_serial
        if not serial:
            return
        def check() -> DeviceDetails:
            device = self.device_manager.get(serial)
            client = AdbClient(serial, adb_path=self.config.adb_path, command_timeout=self.config.command_timeout)
            geometry = GeometryProvider(client).read()
            details = DeviceDetails(device, client.shell("getprop", "ro.build.version.release").strip(), client.shell("getprop", "ro.build.version.sdk").strip(), f"{geometry.width}x{geometry.height}, d{geometry.density or '-'}, r{geometry.rotation}")
            health = DeviceHealthMonitor(client, self.config.package).assess()
            return details, health  # type: ignore[return-value]
        def done(details: object) -> None:
            self._show_details(details)  # type: ignore[arg-type]
        self._submit(check, done, f"Checking {serial}...")

    def _show_details(self, details: DeviceDetails, health: object | None = None) -> None:
        if isinstance(details, tuple):
            details, health = details
        self._known_devices[details.device.serial] = details.device
        self._select_device(details.device.serial)
        self.detail_values["Android"].configure(text=details.android_version)
        self.detail_values["SDK"].configure(text=details.sdk)
        self.focus_info_values["Geometry"].configure(text=details.geometry)
        self._update_focus_view()
        self._log(f"CHECK  {details.device.serial}  Android {details.android_version}, SDK {details.sdk}")
        if health is not None:
            blockers = getattr(health, "blockers", ())
            self._log(f"HEALTH  {'safe' if not blockers else 'blocked'}  blockers={','.join(blockers) or 'none'}")
            if blockers:
                self.detail_state.configure(text="; ".join(blockers), style="Error.TLabel")
        self._set_status("Device check completed")

    def _open_scrcpy(self) -> None:
        if self._game_safe_mode:
            self._set_status("Game Safe Mode is active; resume controller first")
            return
        serial = self._current_serial
        if not serial or serial in self._opening_scrcpy:
            return
        if self.scrcpy_manager.status(serial) == "running":
            self._set_status("scrcpy already running")
            return
        profile = self.scrcpy_profile.get()
        if profile == "recording":
            self._handle_error("Recording profile requires a record path; use CLI scrcpy --record")
            return
        audio = bool(self.scrcpy_audio.get())
        clipboard_autosync = bool(self.scrcpy_clipboard.get())
        stay_awake = bool(self.scrcpy_stay_awake.get())
        self._opening_scrcpy.add(serial)
        self.open_button.configure(state="disabled")
        def start() -> object:
            return self.scrcpy_manager.start(
                serial,
                profile,
                audio=audio,
                clipboard_autosync=clipboard_autosync,
                stay_awake=stay_awake,
            )
        def done(session: object) -> None:
            self._opening_scrcpy.discard(serial)
            self._active_scrcpy_serials.add(serial)
            pid = session.process.pid  # type: ignore[attr-defined]
            self.stop_button.configure(state="normal")
            self.record_button.configure(state="normal")
            self.quick_record_button.configure(state="normal", text="Record", command=self._start_recording)
            self._log(f"SCRCPY START  {serial}  profile={profile}  pid={pid}")
            self._set_status("scrcpy is running")
        self._submit(start, done, f"Opening scrcpy for {serial}...")

    def _stop_scrcpy(self) -> None:
        serial = self._current_serial
        if not serial:
            return
        def stop() -> bool:
            return self.scrcpy_manager.stop(serial)
        def done(stopped: object) -> None:
            self._stop_recording(save=False)
            self._active_scrcpy_serials.discard(serial)
            self.stop_button.configure(state="disabled")
            self.open_button.configure(state="normal")
            self._log(f"SCRCPY STOP  {serial}  stopped={stopped}")
            self._set_status("scrcpy stopped")
        self._submit(stop, done, f"Stopping scrcpy for {serial}...")

    def _copy_serial(self) -> None:
        if self._current_serial:
            self.clipboard_clear()
            self.clipboard_append(self._current_serial)
            self._set_status(f"Copied {self._current_serial}")

    def _reconnect_adb(self) -> None:
        self.reconnect_button.configure(state="disabled")
        def reconnect() -> object:
            return self.device_manager.reconnect()
        def done(_result: object) -> None:
            self.reconnect_button.configure(state="normal")
            self._log("ADB RECONNECT  daemon restarted")
            self._set_status("ADB reconnected; scanning devices")
            self._poll_devices()
        def failed(message: str) -> None:
            self.reconnect_button.configure(state="normal")
            self._handle_error(message)
        self._submit(reconnect, done, "Reconnecting ADB...", failed)

    def _edit_device_metadata(self) -> None:
        serial = self._current_serial
        if not serial:
            return
        meta = self._meta(serial)
        dialog = tk.Toplevel(self)
        dialog.title(f"Device metadata - {serial}")
        dialog.transient(self)
        dialog.grab_set()
        dialog.resizable(False, False)
        form = ttk.Frame(dialog, padding=16)
        form.pack(fill="both", expand=True)
        fields: dict[str, ttk.Entry] = {}
        for row, key in enumerate(("name", "group", "role", "location", "environment", "tags")):
            ttk.Label(form, text=key.title()).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=5)
            entry = ttk.Entry(form, width=34)
            current = meta.get(key, [])
            entry.insert(0, ", ".join(current) if key == "tags" and isinstance(current, list) else str(current))
            entry.grid(row=row, column=1, sticky="ew", pady=5)
            fields[key] = entry
        form.columnconfigure(1, weight=1)
        buttons = ttk.Frame(form)
        buttons.grid(row=6, column=0, columnspan=2, sticky="e", pady=(12, 0))
        def save() -> None:
            environment = fields["environment"].get().strip().lower() or "test"
            if environment not in {"test", "production"}:
                self._handle_error("Environment must be test or production")
                return
            self._device_meta[serial] = {
                "name": fields["name"].get().strip(),
                "group": fields["group"].get().strip(),
                "role": fields["role"].get().strip(),
                "location": fields["location"].get().strip(),
                "environment": environment,
                "tags": [tag.strip() for tag in fields["tags"].get().split(",") if tag.strip()],
            }
            self._write_device_metadata()
            dialog.destroy()
            self._update_devices(list(self._known_devices.values()))
            self._select_device(serial)
            self._log(f"METADATA SAVE  {serial}")
        ttk.Button(buttons, text="Cancel", style="Secondary.TButton", command=dialog.destroy).pack(side="right")
        ttk.Button(buttons, text="Save", style="Primary.TButton", command=save).pack(side="right", padx=(0, 6))

    def _start_recording(self) -> None:
        serial = self._current_serial
        if not serial or self._recorder is not None:
            return
        sessions = {session.serial: session for session in self.scrcpy_manager.sessions()}
        session = sessions.get(serial)
        if session is None:
            self._handle_error("Open scrcpy from this GUI before recording")
            return
        try:
            geometry = GeometryProvider(self._gui_client()).read()  # type: ignore[arg-type]
            self._recorded_workflow = RecordedWorkflow(metadata={"serial": serial, "width": geometry.width, "height": geometry.height, "density": geometry.density, "rotation": geometry.rotation})
            self._recorder_steps = self._recorded_workflow.steps
            self._recorder_selected_index = None
            self._refresh_recorder_events()
            self._recorder = MouseRecorder(session.process.pid, geometry, self._record_gesture)
            self._recorder.start()
        except Exception as exc:
            self._handle_error(str(exc))
            self._recorder = None
            return
        self.record_button.configure(state="disabled")
        self.quick_record_button.configure(state="normal", text="Stop & Save", command=self._stop_recording)
        self.save_recording_button.configure(state="normal")
        self.add_record_text_button.configure(state="normal")
        self.recorder_state.configure(text="Recording mouse inside scrcpy", style="Warning.TLabel")
        self._log(f"RECORDER START  serial={serial}  pid={session.process.pid}")
        self.after(RECORDER_POLL_MS, self._poll_recorder)

    def _poll_recorder(self) -> None:
        if self._recorder is None:
            return
        try:
            self._recorder.sample()
        except Exception as exc:
            self._handle_error(f"Recorder stopped: {exc}")
            self._stop_recording(save=False)
            return
        self.after(RECORDER_POLL_MS, self._poll_recorder)

    def _record_gesture(self, gesture: MouseGesture) -> None:
        if self._recorded_workflow is None:
            return
        self._recorded_workflow.add_gesture(gesture)
        self._recorder_steps = self._recorded_workflow.steps
        self._queue(self._refresh_recorder_events)
        self._queue(lambda: self._log(f"RECORDED  {gesture.kind}  ({gesture.x},{gesture.y})"))

    def _add_recorded_text(self) -> None:
        if self._recorded_workflow is None:
            return
        value = self.record_text_entry.get()
        if not value or value == "Text step":
            self._handle_error("Enter text before adding it to the recording")
            return
        self._recorded_workflow.add_text(value)
        self._recorder_steps = self._recorded_workflow.steps
        self._refresh_recorder_events()
        self.record_text_entry.delete(0, "end")
        self._log("RECORDED  text")

    def _refresh_recorder_events(self) -> None:
        if not hasattr(self, "recorder_events"):
            return
        for item in self.recorder_events.get_children():
            self.recorder_events.delete(item)
        for index, step in enumerate(self._recorder_steps):
            kind = str(step.get("kind", ""))
            payload = step.get("action" if kind == "action" else "condition", {})
            if kind == "screenshot":
                payload = {"screenshot_name": step.get("screenshot_name", "")}
            self.recorder_events.insert("", "end", iid=str(index), values=(kind, step.get("name", f"step_{index + 1}"), json.dumps(payload, ensure_ascii=False, separators=(",", ":"))))
        if hasattr(self, "recorder_scope"):
            current = self._recorded_workflow.metadata if self._recorded_workflow is not None else {}
            serial = str(current.get("serial", self._current_serial or "-"))
            geometry = "x".join(str(current.get(key, "-")) for key in ("width", "height"))
            self.recorder_scope.configure(text=f"Target: {serial}  |  Geometry: {geometry}  |  {len(self._recorder_steps)} events")
        if self._recorder_selected_index is not None and self._recorder_selected_index < len(self._recorder_steps):
            item = str(self._recorder_selected_index)
            self.recorder_events.selection_set(item)
            self.recorder_events.see(item)

    def _recorder_select(self, _event: object) -> None:
        selected = self.recorder_events.selection()
        self._recorder_selected_index = int(selected[0]) if selected else None
        self.recorder_event_payload.delete("1.0", "end")
        if self._recorder_selected_index is not None:
            self.recorder_event_payload.insert("1.0", json.dumps(self._recorder_steps[self._recorder_selected_index], ensure_ascii=False, indent=2))

    def _recorder_update_event(self) -> None:
        if self._recorder_selected_index is None:
            self._handle_error("Select a recorded event first")
            return
        try:
            event = json.loads(self.recorder_event_payload.get("1.0", "end").strip())
            if not isinstance(event, dict) or event.get("kind") not in {"action", "wait", "assert", "screenshot", "stop"}:
                raise ValueError("Event must be a step object with a valid kind")
        except Exception as exc:
            self._handle_error(f"Invalid event JSON: {exc}")
            return
        self._recorder_steps[self._recorder_selected_index] = event
        if self._recorded_workflow is not None:
            self._recorded_workflow.steps = self._recorder_steps
        self._refresh_recorder_events()
        self._set_status("Recorded event updated")

    def _recorder_delete_event(self) -> None:
        if self._recorder_selected_index is None:
            self._handle_error("Select a recorded event first")
            return
        del self._recorder_steps[self._recorder_selected_index]
        self._recorder_selected_index = min(self._recorder_selected_index, len(self._recorder_steps) - 1) if self._recorder_steps else None
        if self._recorded_workflow is not None:
            self._recorded_workflow.steps = self._recorder_steps
        self._refresh_recorder_events()
        self._set_status("Recorded event deleted")

    def _recorder_move(self, direction: int) -> None:
        index = self._recorder_selected_index
        if index is None:
            self._handle_error("Select a recorded event first")
            return
        target = index + direction
        if not 0 <= target < len(self._recorder_steps):
            return
        self._recorder_steps[index], self._recorder_steps[target] = self._recorder_steps[target], self._recorder_steps[index]
        self._recorder_selected_index = target
        if self._recorded_workflow is not None:
            self._recorded_workflow.steps = self._recorder_steps
        self._refresh_recorder_events()

    def _recorder_checkpoint(self) -> None:
        if self._recorded_workflow is None:
            self._handle_error("Start recording before adding a checkpoint")
            return
        self._recorded_workflow.add_checkpoint()
        self._recorder_steps = self._recorded_workflow.steps
        self._refresh_recorder_events()
        self._log("RECORDED  screenshot checkpoint")

    def _stop_recording(self, save: bool = True) -> None:
        recorder, workflow = self._recorder, self._recorded_workflow
        self._recorder = None
        self.save_recording_button.configure(state="disabled")
        self.add_record_text_button.configure(state="disabled")
        self.quick_record_button.configure(text="Record", command=self._start_recording)
        if recorder:
            recorder.stop()
        if not save or workflow is None or not workflow.steps:
            self.recorder_state.configure(text="Recorder idle", style="Info.TLabel")
            self._refresh_recorder_events()
            return
        serial = self._current_serial or "unknown"
        path = self._gui_session_dir(serial) / f"recording_{time.strftime('%Y%m%d_%H%M%S')}.json"
        workflow.save(path, self.workflow_package_var.get().strip() or None)
        self.workflow_path.set(str(path))
        self.recorder_state.configure(text=f"Saved {len(workflow.steps)} steps", style="Ready.TLabel")
        self._log(f"RECORDER SAVE  {path}")
        self._set_status("Recording saved; run it with Dry-run first")
        self._recorded_workflow = None
        if self.scrcpy_manager.status(serial) == "running":
            self.record_button.configure(state="normal")
            self.quick_record_button.configure(state="normal")

    def _open_latest_log_artifact(self) -> None:
        path = self._latest_log_artifact
        if path is None or not path.exists():
            self._handle_error("No existing artifact is available from the current log filter")
            return
        try:
            os.startfile(str(path))
        except AttributeError:
            self._set_status(f"Artifact: {path}")
        except OSError as exc:
            self._handle_error(f"Cannot open artifact: {exc}")
        else:
            self._set_status(f"Opened artifact: {path.name}")

    def _update_input_state(self) -> None:
        if self._game_safe_mode:
            for button in (self.tap_button, self.swipe_button, self.key_button, self.text_button, self.run_workflow_button):
                button.configure(state="disabled")
            self.input_hint.configure(text="Game Safe Mode: controller paused", style="Warning.TLabel")
            self.automation_input_hint.configure(text="Controller paused", style="Warning.TLabel")
            self.workflow_mode.configure(text="PAUSED - ADB and device input stopped", style="Warning.TLabel")
            self.workflow_hint.configure(text="Resume controller to continue", style="Warning.TLabel")
            return
        if self._safety_latched:
            for button in (self.tap_button, self.swipe_button, self.key_button, self.text_button, self.run_workflow_button):
                button.configure(state="disabled")
            self.input_hint.configure(text="Safety stopped", style="Error.TLabel")
            self.automation_input_hint.configure(text="Safety stopped", style="Error.TLabel")
            self.workflow_mode.configure(text="SAFETY STOPPED - reset safety to continue", style="Error.TLabel")
            self.workflow_hint.configure(text="All mutating actions are blocked", style="Error.TLabel")
            return
        enabled = self.dry_run_var.get() or self.confirm_var.get()
        state = "normal" if enabled else "disabled"
        for button in (self.tap_button, self.swipe_button, self.key_button, self.text_button):
            button.configure(state=state)
        if self.dry_run_var.get():
            self.input_hint.configure(text="Dry-run: no device changes", style="Info.TLabel")
            self.automation_input_hint.configure(text="Simulation only", style="Info.TLabel")
            self.run_workflow_button.configure(text="Simulate")
            self.workflow_hint.configure(text="Simulation mode: recorded input is skipped", style="Info.TLabel")
            self.workflow_mode.configure(text="SIMULATION - no input will be sent", style="Info.TLabel")
        elif self.confirm_var.get():
            self.input_hint.configure(text="Confirmed input enabled", style="Warning.TLabel")
            self.automation_input_hint.configure(text="Live input enabled", style="Warning.TLabel")
            self.run_workflow_button.configure(text="Run live")
            self.workflow_hint.configure(text="Live replay: input will be sent to device", style="Warning.TLabel")
            self.workflow_mode.configure(text="LIVE REPLAY - device input enabled", style="Warning.TLabel")
        else:
            self.input_hint.configure(text="Input blocked until confirmed", style="Warning.TLabel")
            self.automation_input_hint.configure(text="Live input blocked", style="Warning.TLabel")
            self.run_workflow_button.configure(text="Run live (confirm required)")
            self.workflow_hint.configure(text="Live replay blocked until Confirm input", style="Warning.TLabel")
            self.workflow_mode.configure(text="BLOCKED - enable Confirm input", style="Error.TLabel")

    def _gui_client(self) -> AdbClient | None:
        if self._game_safe_mode:
            self._set_status("Game Safe Mode is active; resume controller first")
            return None
        if not self._current_serial:
            self._handle_error("Select a ready device first")
            return None
        device = self._known_devices.get(self._current_serial)
        if not device or not device.ready:
            self._handle_error("Selected device is not ready")
            return None
        return AdbClient(self._current_serial, adb_path=self.config.adb_path, command_timeout=self.config.command_timeout)

    def _gui_session_dir(self, serial: str) -> Path:
        session = self.config.artifacts_dir / serial / time.strftime("%Y%m%d_%H%M%S")
        session.mkdir(parents=True, exist_ok=True)
        return session

    @staticmethod
    def _workflow_payload(result: object) -> dict[str, object]:
        return {
            "status": result.status,  # type: ignore[attr-defined]
            "ok": result.ok,  # type: ignore[attr-defined]
            "steps": [
                {
                    "name": step.name,
                    "kind": step.kind,
                    "status": step.status,
                    "attempts": step.attempts,
                    "elapsed_seconds": step.elapsed_seconds,
                    "artifact": str(step.artifact) if step.artifact else None,
                    "error": step.error,
                }
                for step in result.steps  # type: ignore[attr-defined]
            ],
        }

    def _prepare_workflow_progress(self, steps: list[object]) -> None:
        for item in self.workflow_steps.get_children():
            self.workflow_steps.delete(item)
        self._workflow_step_ids = {}
        for index, step in enumerate(steps, start=1):
            item_id = self.workflow_steps.insert("", "end", values=(step.kind, "Pending", "-", "-", ""))
            self._workflow_step_ids[step.name] = item_id
        self.workflow_progress.configure(maximum=max(1, len(steps)), value=0)
        self.workflow_counts.configure(text=f"0/{len(steps)} steps")
        self.workflow_current.configure(text="Preparing workflow")

    def _workflow_step_started(self, step: object, index: int, total: int, loop_index: int | None = None, loop_total: int | None = None) -> None:
        loop_index = loop_index or self._workflow_loop_index
        loop_total = loop_total or self._workflow_loop_total
        prefix = f"Loop {loop_index}/{loop_total} | " if loop_total > 1 else ""
        item_id = self._workflow_step_ids.get(step.name)
        if item_id:
            values = list(self.workflow_steps.item(item_id, "values"))
            values[1] = "Running"
            self.workflow_steps.item(item_id, values=values, tags=("running",))
        self.workflow_current.configure(text=f"{prefix}Running: {step.name}")
        self.workflow_counts.configure(text=f"{prefix}{index - 1}/{total} steps")
        self.workflow_progress.configure(maximum=max(1, total), value=index - 1)

    def _workflow_step_finished(self, step: object, result: object, index: int, total: int, loop_index: int | None = None, loop_total: int | None = None) -> None:
        loop_index = loop_index or self._workflow_loop_index
        loop_total = loop_total or self._workflow_loop_total
        prefix = f"Loop {loop_index}/{loop_total} | " if loop_total > 1 else ""
        item_id = self._workflow_step_ids.get(step.name)
        if item_id:
            elapsed = f"{result.elapsed_seconds:.2f}s"
            values = (step.kind, result.status.title(), result.attempts, elapsed, result.error or "")
            self.workflow_steps.item(item_id, values=values, tags=(result.status,))
        self.workflow_progress.configure(maximum=max(1, total), value=index)
        self.workflow_counts.configure(text=f"{prefix}{index}/{total} steps")
        self.workflow_current.configure(text=f"{prefix}Finished: {step.name} ({result.status})")
        self._log(f"STEP  {prefix}{step.name}  {result.status}  attempts={result.attempts}  elapsed={result.elapsed_seconds:.2f}s")

    def _browse_workflow(self) -> None:
        selected = filedialog.askopenfilename(
            title="Choose workflow JSON",
            initialdir=str(Path(self.workflow_path.get()).parent),
            filetypes=(("Workflow JSON", "*.json"), ("All files", "*.*")),
        )
        if selected:
            self.workflow_path.set(selected)
            self._load_editor_workflow(silent=True)

    def _load_editor_workflow(self, silent: bool = False) -> None:
        path = Path(self.workflow_path.get())
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or not isinstance(raw.get("steps"), list):
                raise ValueError("Workflow JSON phải có mảng steps")
            if not all(isinstance(step, dict) for step in raw["steps"]):
                raise ValueError("Mỗi step phải là object JSON")
        except Exception as exc:
            if not silent:
                self._handle_error(f"Cannot load workflow: {exc}")
            return
        self.editor_spec = raw
        self.editor_steps_data = list(raw["steps"])
        self.editor_selected_index = None
        self._editor_refresh()
        if not silent:
            self._set_status(f"Loaded {len(self.editor_steps_data)} steps")

    @staticmethod
    def _editor_summary(step: dict[str, object]) -> str:
        kind = str(step.get("kind", ""))
        payload = step.get("action" if kind == "action" else "condition", {})
        if kind == "screenshot":
            payload = {"screenshot_name": step.get("screenshot_name", "")}
        if kind == "stop":
            payload = {}
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def _editor_refresh(self) -> None:
        for item in self.editor_tree.get_children():
            self.editor_tree.delete(item)
        for index, step in enumerate(self.editor_steps_data):
            self.editor_tree.insert("", "end", iid=str(index), values=(step.get("name", f"step_{index + 1}"), step.get("kind", ""), self._editor_summary(step)))
        if self.editor_selected_index is not None and 0 <= self.editor_selected_index < len(self.editor_steps_data):
            item = str(self.editor_selected_index)
            self.editor_tree.selection_set(item)
            self.editor_tree.see(item)
            self._editor_load_form(self.editor_selected_index)
        else:
            self._editor_clear_form()

    def _editor_clear_form(self) -> None:
        self.editor_name.delete(0, "end")
        self.editor_kind.set("")
        self.editor_timeout.delete(0, "end")
        self.editor_retries.delete(0, "end")
        self.editor_payload.delete("1.0", "end")

    def _editor_load_form(self, index: int) -> None:
        step = self.editor_steps_data[index]
        self.editor_name.delete(0, "end")
        self.editor_name.insert(0, str(step.get("name", f"step_{index + 1}")))
        self.editor_kind.set(str(step.get("kind", "action")))
        self.editor_timeout.delete(0, "end")
        self.editor_timeout.insert(0, str(step.get("timeout", 10.0)))
        self.editor_retries.delete(0, "end")
        self.editor_retries.insert(0, str(step.get("retries", 0)))
        kind = str(step.get("kind", ""))
        if kind == "action":
            payload = step.get("action", {})
        elif kind in {"wait", "assert"}:
            payload = step.get("condition", {})
        elif kind == "screenshot":
            payload = {"screenshot_name": step.get("screenshot_name", "")}
        else:
            payload = {}
        self.editor_payload.delete("1.0", "end")
        self.editor_payload.insert("1.0", json.dumps(payload, ensure_ascii=False, indent=2))

    def _editor_select(self, _event: object) -> None:
        selected = self.editor_tree.selection()
        self.editor_selected_index = int(selected[0]) if selected else None
        if self.editor_selected_index is not None:
            self._editor_load_form(self.editor_selected_index)

    def _editor_read_form(self) -> dict[str, object]:
        if self.editor_selected_index is None:
            raise ValueError("Select a step first")
        name = self.editor_name.get().strip()
        kind = self.editor_kind.get().strip()
        if not name or kind not in {"action", "wait", "assert", "screenshot", "stop"}:
            raise ValueError("Step name/kind không hợp lệ")
        timeout = float(self.editor_timeout.get())
        retries = int(self.editor_retries.get())
        if timeout <= 0 or retries < 0:
            raise ValueError("Timeout phải > 0 và retries >= 0")
        try:
            payload = json.loads(self.editor_payload.get("1.0", "end").strip() or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"Payload JSON không hợp lệ: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("Payload phải là JSON object")
        previous = self.editor_steps_data[self.editor_selected_index]
        updated: dict[str, object] = {"name": name, "kind": kind, "timeout": timeout, "retries": retries}
        if "poll_interval" in previous:
            updated["poll_interval"] = previous["poll_interval"]
        if kind == "action":
            updated["action"] = payload
        elif kind in {"wait", "assert"}:
            updated["condition"] = payload
        elif kind == "screenshot":
            updated["screenshot_name"] = str(payload.get("screenshot_name", name))
        return updated

    def _editor_update(self) -> bool:
        try:
            self.editor_steps_data[self.editor_selected_index] = self._editor_read_form()  # type: ignore[index]
        except Exception as exc:
            self._handle_error(str(exc))
            return False
        self._editor_refresh()
        self._set_status("Step updated (save JSON to persist)")
        return True

    def _editor_add(self) -> None:
        if self.editor_spec is None:
            self._load_editor_workflow()
            if self.editor_spec is None:
                return
        self.editor_steps_data.append({"name": f"step_{len(self.editor_steps_data) + 1}", "kind": "action", "action": {"type": "tap", "x": 0, "y": 0}, "timeout": 10.0, "retries": 0})
        self.editor_selected_index = len(self.editor_steps_data) - 1
        self._editor_refresh()
        self._set_status("New step added")

    def _editor_delete(self) -> None:
        if self.editor_selected_index is None:
            self._handle_error("Select a step first")
            return
        del self.editor_steps_data[self.editor_selected_index]
        self.editor_selected_index = min(self.editor_selected_index, len(self.editor_steps_data) - 1) if self.editor_steps_data else None
        self._editor_refresh()
        self._set_status("Step deleted (save JSON to persist)")

    def _editor_move(self, direction: int) -> None:
        index = self.editor_selected_index
        if index is None:
            self._handle_error("Select a step first")
            return
        target = index + direction
        if not 0 <= target < len(self.editor_steps_data):
            return
        self.editor_steps_data[index], self.editor_steps_data[target] = self.editor_steps_data[target], self.editor_steps_data[index]
        self.editor_selected_index = target
        self._editor_refresh()

    def _editor_save(self) -> None:
        if self.editor_spec is None:
            self._handle_error("Load a workflow before saving")
            return
        try:
            if self.editor_selected_index is not None and not self._editor_update():
                return
            payload = {key: value for key, value in self.editor_spec.items() if key != "_base_dir"}
            payload["steps"] = self.editor_steps_data
            target_package = self.workflow_package_var.get().strip()
            if target_package:
                variables = payload.get("variables", {})
                variables = dict(variables) if isinstance(variables, dict) else {}
                variables["PACKAGE"] = target_package
                payload["variables"] = variables
            path = Path(self.workflow_path.get())
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            self.editor_spec = payload
            self._log(f"EDITOR SAVE  {path}")
            self._set_status(f"Saved workflow: {path.name}")
        except Exception as exc:
            self._handle_error(f"Cannot save workflow: {exc}")

    def _resolve_workflow_package(self, spec: dict[str, object]) -> str | None:
        """Resolve an optional per-workflow package without making config.package mandatory for input actions."""
        explicit = self.workflow_package_var.get().strip()
        variables = spec.get("variables", {})
        variable_package = variables.get("PACKAGE", "") if isinstance(variables, dict) else ""
        if not variable_package:
            variable_package = spec.get("package", "")
        package = explicit or str(variable_package).strip()
        if not package:
            return None
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_*]+)+", package):
            raise ValueError("Target app package không hợp lệ; dùng dạng com.example.app hoặc để trống")
        if not explicit and package:
            self.workflow_package_var.set(package)
        return package

    def _workflow_repeat_count(self) -> int:
        try:
            count = int(self.workflow_repeat_var.get())
        except (TypeError, ValueError) as exc:
            raise ValueError("Repeat workflow phải là số nguyên từ 1 đến 999") from exc
        if not 1 <= count <= 999:
            raise ValueError("Repeat workflow phải trong khoảng 1 đến 999")
        return count

    def _run_workflow_gui(self) -> None:
        if self._game_safe_mode:
            self._set_status("Game Safe Mode is active; resume controller first")
            return
        if self._safety_latched:
            self._handle_error("Emergency stop is active; reset safety first")
            return
        if self._workflow_context is not None:
            self._set_status("A workflow is already running")
            return
        client = self._gui_client()
        if not client:
            return
        try:
            spec = load_spec(self.workflow_path.get())
            target_package = self._resolve_workflow_package(spec)
            repeat_count = self._workflow_repeat_count()
            dry_run = bool(self.dry_run_var.get())
            confirmed = bool(self.confirm_var.get())
            if has_mutating_actions(spec) and not dry_run and not confirmed:
                raise ValueError("Enable Confirm input or Dry-run before this workflow")
            if has_mutating_actions(spec) and not dry_run:
                if not messagebox.askyesno(
                    "Confirm live replay",
                    f"This workflow will send device input to {client.serial}.\n\nWorkflow: {Path(self.workflow_path.get()).name}\n\nContinue?",
                    parent=self,
                ):
                    self._set_status("Live replay cancelled")
                    return
            steps = build_steps(spec)
        except Exception as exc:
            self._handle_error(str(exc))
            return
        session = self._gui_session_dir(client.serial)
        self._workflow_loop_index = 1
        self._workflow_loop_total = repeat_count
        self._prepare_workflow_progress(steps)
        context = WorkflowContext(client, session, {"dry_run": dry_run, "workflow_base_dir": spec["_base_dir"]})
        context.data["capture_actions"] = bool(self.config.capture_actions)
        try:
            context.data["geometry"] = GeometryProvider(client).read()
        except Exception as exc:
            context.data["geometry_error"] = str(exc)
        if not dry_run:
            context.data["health_guard"] = DeviceHealthMonitor(client, target_package)
        recording_geometry = spec.get("recording", {})
        current_geometry = context.data.get("geometry")
        if isinstance(recording_geometry, dict) and current_geometry is not None and recording_geometry:
            mismatches = [
                key for key in ("width", "height", "density", "rotation")
                if recording_geometry.get(key) is not None and recording_geometry.get(key) != getattr(current_geometry, key)
            ]
            if mismatches:
                warning = "Recording geometry differs: " + ", ".join(mismatches)
                self._log("WARNING  " + warning)
                self.workflow_hint.configure(text=warning, style="Warning.TLabel")
        self._workflow_context = context
        self.run_workflow_button.configure(state="disabled")
        self.stop_workflow_button.configure(state="normal")
        self.pause_workflow_button.configure(state="normal", text="Pause")
        workflow_file = Path(self.workflow_path.get())
        launch_target = bool(self.launch_app_var.get())

        def run() -> object:
            log = session / "run.log"
            log.write_text(f"workflow={workflow_file}\ndry_run={dry_run}\ntarget_package={target_package or '-'}\nrepeat_count={repeat_count}\nlaunch_target={launch_target}\n", encoding="utf-8")
            if launch_target and not target_package:
                raise ValueError("Launch target app requires a target package; enter one or disable launch")
            if launch_target and not dry_run:
                client.run("shell", "monkey", "-p", target_package, "1", retries=0)
                time.sleep(1.0)
            all_steps = []
            last_result = None
            for loop_index in range(1, repeat_count + 1):
                if loop_index > 1:
                    self._queue(lambda: self._prepare_workflow_progress(steps))
                self._workflow_loop_index = loop_index
                loop_steps = build_steps(spec)
                def step_started(step, index, total, current_loop=loop_index):
                    self._queue(lambda: self._workflow_step_started(step, index, total, current_loop, repeat_count))

                def step_finished(step, result, index, total, current_loop=loop_index):
                    self._queue(lambda: self._workflow_step_finished(step, result, index, total, current_loop, repeat_count))

                last_result = WorkflowRunner(context, on_step_start=step_started, on_step_result=step_finished).run(loop_steps)
                all_steps.extend(last_result.steps)
                if last_result.status != "passed":
                    break
            result = WorkflowResult(last_result.status if last_result is not None else "failed", tuple(all_steps))
            payload = self._workflow_payload(result)
            action_steps = [step for step in result.steps if step.kind == "action"]
            loops_completed = min(repeat_count, next((index for index, step in enumerate(all_steps) if step.status in {"failed", "stopped"}), repeat_count * len(steps)) // max(1, len(steps)))
            if result.status == "passed":
                loops_completed = repeat_count
            payload.update({"serial": client.serial, "dry_run": dry_run, "workflow": str(workflow_file), "target_package": target_package, "repeat_count": repeat_count, "loops_completed": loops_completed, "actions_total": len(action_steps), "actions_passed": sum(step.status == "passed" for step in action_steps), "launch_target": launch_target})
            payload["simulated_actions"] = len(context.data.get("dry_run_actions", []))
            payload["action_artifacts"] = context.data.get("action_artifacts", [])
            if result.status == "failed":
                try:
                    payload["failure_screenshot"] = str(client.save_screenshot(session / "failure.png"))
                except Exception as exc:
                    payload["failure_screenshot_error"] = str(exc)
            report = session / "result.json"
            report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            with log.open("a", encoding="utf-8") as handle:
                handle.write(f"status={result.status}\nloops_completed={loops_completed}\nreport={report}\n")
            return payload | {"report": str(report)}

        def done(payload: object) -> None:
            self._workflow_context = None
            self.run_workflow_button.configure(state="normal")
            self.stop_workflow_button.configure(state="disabled")
            self.pause_workflow_button.configure(state="disabled", text="Pause")
            status = payload["status"]  # type: ignore[index]
            failed_steps = [step for step in payload["steps"] if step["status"] == "failed"]  # type: ignore[index]
            if failed_steps:
                self._log(f"WORKFLOW ERROR  {failed_steps[0]['name']}: {failed_steps[0]['error']}")
            if payload["dry_run"]:  # type: ignore[index]
                count = payload["simulated_actions"]  # type: ignore[index]
                self._log(f"SIMULATION  {status}  skipped_actions={count}  report={payload['report']}")  # type: ignore[index]
                self._set_status(f"Simulation {status}: no input sent")
            else:
                self._log(f"WORKFLOW  {status}  actions={payload['actions_passed']}/{payload['actions_total']}  report={payload['report']}")  # type: ignore[index]
                self._set_status(f"Workflow {status}: {payload['actions_passed']}/{payload['actions_total']} actions sent")  # type: ignore[index]
            self.workflow_current.configure(text=f"Workflow {status} ({payload.get('loops_completed', 0)}/{payload.get('repeat_count', 1)} loops)")
            self._update_input_state()
        def failed(message: str) -> None:
            self._workflow_context = None
            self.run_workflow_button.configure(state="normal")
            self.stop_workflow_button.configure(state="disabled")
            self.pause_workflow_button.configure(state="disabled", text="Pause")
            self._handle_error(message)
            self._update_input_state()
        self._submit(run, done, f"Running workflow: {workflow_file.name}", failed)

    def _stop_workflow_gui(self) -> None:
        if self._workflow_context is None:
            return
        self._workflow_context.stop_requested = True
        self.stop_workflow_button.configure(state="disabled")
        self._log("WORKFLOW STOP  requested")
        self._set_status("Stopping workflow after current operation")

    def _enqueue_selected_workflow(self) -> None:
        if self._workflow_queue is not None:
            self._handle_error("A workflow queue is already running")
            return
        serials = sorted(self._selected_serials)
        if not serials:
            self._handle_error("Select one or more devices in Devices before queuing")
            return
        try:
            spec = load_spec(self.workflow_path.get())
            target_package = self._resolve_workflow_package(spec)
            repeat_count = self._workflow_repeat_count()
            dry_run = bool(self.dry_run_var.get())
            if has_mutating_actions(spec) and not dry_run and not self.confirm_var.get():
                raise ValueError("Enable Confirm input or Dry-run before live queue")
            if has_mutating_actions(spec) and not dry_run:
                scope = "\n".join(serials)
                if not messagebox.askyesno("Confirm live queue", f"This workflow will send input to {len(serials)} device(s):\n\n{scope}\n\nContinue?", parent=self):
                    self._set_status("Queue cancelled")
                    return
        except Exception as exc:
            self._handle_error(str(exc))
            return
        self._workflow_queue = WorkflowQueue(max_concurrency=2)
        for item in self.queue_tree.get_children():
            self.queue_tree.delete(item)
        for serial in serials:
            self.queue_tree.insert("", "end", iid=serial, values=(serial, "Queued", "", ""), tags=("queued",))
        self.queue_summary.configure(text=f"{len(serials)} queued | concurrency 2")
        self.enqueue_button.configure(state="disabled")
        self.queue_pause_button.configure(state="normal", text="Pause queue")
        self.queue_stop_button.configure(state="normal")
        workflow_file = Path(self.workflow_path.get())

        def update(item: QueueItem) -> None:
            snapshot = (item.serial, item.status, item.report or "", item.error or "")
            self._queue(lambda: self._queue_item_update(*snapshot))

        def run_job(serial: str, control: QueueControl) -> str:
            client = AdbClient(serial, self.config.adb_path, self.config.command_timeout)
            session = self._gui_session_dir(serial)
            context = WorkflowContext(client, session, {"dry_run": dry_run, "workflow_base_dir": spec["_base_dir"], "capture_actions": bool(self.config.capture_actions)})
            control.register(context)
            if not dry_run:
                context.data["health_guard"] = DeviceHealthMonitor(client, target_package)
            try:
                context.data["geometry"] = GeometryProvider(client).read()
            except Exception as exc:
                context.data["geometry_error"] = str(exc)
            all_steps = []
            last_result = None
            step_count = len(spec.get("steps", []))
            for loop_index in range(1, repeat_count + 1):
                last_result = WorkflowRunner(context).run(build_steps(spec))
                all_steps.extend(last_result.steps)
                if last_result.status != "passed":
                    break
            result = WorkflowResult(last_result.status if last_result is not None else "failed", tuple(all_steps))
            payload = self._workflow_payload(result)
            loops_completed = repeat_count if result.status == "passed" else max(0, (len(all_steps) - 1) // max(1, step_count))
            payload.update({"serial": serial, "dry_run": dry_run, "workflow": str(workflow_file), "target_package": target_package, "repeat_count": repeat_count, "loops_completed": loops_completed, "queue": True, "action_artifacts": context.data.get("action_artifacts", [])})
            if result.status == "failed":
                try:
                    payload["failure_screenshot"] = str(client.save_screenshot(session / "failure.png"))
                except Exception as exc:
                    payload["failure_screenshot_error"] = str(exc)
            report = session / "queue_result.json"
            report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return str(report)

        def run_queue() -> object:
            assert self._workflow_queue is not None
            self._workflow_queue.run(serials, run_job, update)
            return self._workflow_queue.items

        def done(items: object) -> None:
            results = list(items.values())  # type: ignore[union-attr]
            passed = sum(item.status == "passed" for item in results)
            self.queue_summary.configure(text=f"Queue complete | {passed}/{len(results)} passed")
            self.enqueue_button.configure(state="normal")
            self.queue_pause_button.configure(state="disabled", text="Pause queue")
            self.queue_stop_button.configure(state="disabled")
            self._workflow_queue = None
            self._set_status("Workflow queue complete")
        self._submit(run_queue, done, f"Queueing {len(serials)} workflow(s)")

    def _queue_item_update(self, serial: str, status: str, report: str, error: str) -> None:
        if self.queue_tree.exists(serial):
            self.queue_tree.item(serial, values=(serial, status.title(), report, error), tags=(status,))
        self.queue_summary.configure(text=f"Queue update | {serial} {status}")
        self._log(f"QUEUE  {serial}  {status}  {error or report}")

    def _toggle_queue_pause(self) -> None:
        if self._workflow_queue is None:
            return
        if self._workflow_queue.control.paused:
            self._workflow_queue.resume()
            self.queue_pause_button.configure(text="Pause queue")
            self.queue_summary.configure(text="Queue resumed")
        else:
            self._workflow_queue.pause()
            self.queue_pause_button.configure(text="Resume queue")
            self.queue_summary.configure(text="Queue paused")

    def _stop_queue(self) -> None:
        if self._workflow_queue is None:
            return
        self._workflow_queue.stop()
        self.queue_stop_button.configure(state="disabled")
        self.queue_summary.configure(text="Stopping queue")
        self._log("QUEUE STOP requested")

    def _toggle_workflow_pause(self) -> None:
        if self._workflow_context is None:
            return
        self._workflow_context.pause_requested = not self._workflow_context.pause_requested
        paused = self._workflow_context.pause_requested
        self.pause_workflow_button.configure(text="Resume" if paused else "Pause")
        self.workflow_current.configure(text="Paused" if paused else "Resuming")
        self._log("WORKFLOW  " + ("paused" if paused else "resumed"))

    def _emergency_stop(self) -> None:
        self._safety_latched = True
        self.safety.request_stop()
        if self._workflow_context is not None:
            self._workflow_context.stop_requested = True
            self._workflow_context.pause_requested = False
        self._stop_recording(save=False)
        self.scrcpy_manager.stop_all()
        self.emergency_button.configure(text="Safety stopped", state="disabled")
        self.reset_safety_button.configure(state="normal")
        self._log("EMERGENCY STOP  all workflow/input/scrcpy activity stopped")
        self._set_status("Emergency stop active; reset safety to continue")
        self._update_input_state()

    def _reset_safety(self) -> None:
        self.safety = SafetyController()
        self._safety_latched = False
        self.emergency_button.configure(text="Emergency stop", state="normal", command=self._emergency_stop)
        self.reset_safety_button.configure(state="disabled")
        self._log("SAFETY RESET")
        self._set_status("Safety reset")
        self._update_input_state()

    def _capture_screenshot(self) -> None:
        client = self._gui_client()
        if not client:
            return
        output = self._gui_session_dir(client.serial) / f"screenshot_{time.time_ns() % 1_000_000_000:09d}.png"
        def done(path: object) -> None:
            try:
                from PIL import Image, ImageTk
                image = ImageTk.PhotoImage(Image.open(path))
                self._grid_images[client.serial] = image
            except Exception:
                pass
            self._update_focus_view()
            self._log(f"SCREENSHOT  {path}")
            self._set_status("Screenshot saved")
        self._submit(lambda: client.save_screenshot(output), done, "Capturing screenshot...")

    def _input_allowed(self, label: str) -> bool:
        if self._safety_latched:
            self._handle_error("Emergency stop is active; reset safety first")
            return False
        if self.dry_run_var.get():
            self._log(f"DRY-RUN  {label}  (not sent)")
            self._set_status("Dry-run: input not sent")
            return False
        if not self.confirm_var.get():
            self._handle_error("Enable Confirm input before sending " + label)
            return False
        return True

    def _tap_gui(self) -> None:
        client = self._gui_client()
        if not client:
            return
        try:
            x, y = int(self.tap_x.get()), int(self.tap_y.get())
        except ValueError:
            self._handle_error("Tap coordinates must be integers")
            return
        command = client._argv(("shell", "input", "tap", str(x), str(y)))
        if not self._input_allowed("tap " + str((x, y))):
            return
        self._submit(lambda: client.tap(x, y), lambda result: self._input_done("tap", result), "Sending tap...")

    def _swipe_gui(self) -> None:
        client = self._gui_client()
        if not client:
            return
        try:
            x1, y1, x2, y2, duration = [int(entry.get()) for entry in self.swipe_entries]
        except ValueError:
            self._handle_error("Swipe values must be integers")
            return
        if not self._input_allowed("swipe"):
            return
        self._submit(lambda: client.swipe(x1, y1, x2, y2, duration), lambda result: self._input_done("swipe", result), "Sending swipe...")

    def _keyevent_gui(self) -> None:
        client = self._gui_client()
        if not client:
            return
        key = self.key_entry.get().strip()
        if not key:
            self._handle_error("Keyevent cannot be empty")
            return
        if not self._input_allowed("keyevent"):
            return
        self._submit(lambda: client.keyevent(key), lambda result: self._input_done("keyevent", result), "Sending keyevent...")

    def _text_gui(self) -> None:
        client = self._gui_client()
        if not client:
            return
        value = self.text_entry.get()
        if not value:
            self._handle_error("Text cannot be empty")
            return
        if not self._input_allowed("text"):
            return
        self._submit(lambda: client.text(value), lambda result: self._input_done("text", result), "Sending text...")

    def _input_done(self, label: str, result: object) -> None:
        self._log(f"{label.upper()}  completed")
        self._set_status(f"{label} completed")

    def _run_shell_gui(self) -> None:
        client = self._gui_client()
        if not client:
            return
        try:
            args = shlex.split(self.shell_entry.get())
        except ValueError as exc:
            self._handle_error(f"Invalid shell arguments: {exc}")
            return
        if not args:
            self._handle_error("Shell command cannot be empty")
            return
        def run() -> object:
            return client.run("shell", *args, retries=2, check=False)
        def done(result: object) -> None:
            self._log(f"SHELL  {str(result.stdout).strip() or str(result.stderr).strip()}")  # type: ignore[attr-defined]
            self._set_status("Shell command completed")
        self._submit(run, done, "Running shell command...")

    def _save_preferences(self) -> None:
        try:
            poll = max(1, int(self.poll_seconds_var.get()))
            retention = max(1, int(self.retention_days_var.get()))
        except (TypeError, ValueError):
            self._handle_error("Poll/retention must be positive integers")
            return
        self.scrcpy_profile.set(self.default_profile_var.get())
        self._saved_ui_state.update({"poll_seconds": poll, "retention_days": retention, "theme": self.theme_var.get(), "accent": self.accent_var.get(), "default_profile": self.default_profile_var.get()})
        self._write_ui_state()
        self._log("SETTINGS  preferences saved (live confirmation not saved)")
        self._set_status("Preferences saved")

    def _restore_preferences(self) -> None:
        saved = self._saved_ui_state
        if isinstance(saved.get("poll_seconds"), int):
            self.poll_seconds_var.set(max(1, saved["poll_seconds"]))
        if isinstance(saved.get("retention_days"), int):
            self.retention_days_var.set(max(1, saved["retention_days"]))
        if isinstance(saved.get("theme"), str) and saved["theme"] in {"Light", "Dark"}:
            self.theme_var.set(saved["theme"])
        if isinstance(saved.get("accent"), str):
            self.accent_var.set(saved["accent"])
        if isinstance(saved.get("default_profile"), str) and saved["default_profile"] in {"manual", "low-latency", "recording"}:
            self.default_profile_var.set(saved["default_profile"])
            self.scrcpy_profile.set(saved["default_profile"])

    def _run_diagnostics(self) -> None:
        def run() -> str:
            lines = [f"ADB: {self.config.adb_path}", f"scrcpy: {self.config.scrcpy_path}", f"serial: {self._current_serial or '-'}"]
            try:
                device = self.device_manager.get(self._current_serial)
                client = AdbClient(device.serial, self.config.adb_path, self.config.command_timeout)
                geometry = GeometryProvider(client).read()
                health = DeviceHealthMonitor(client, self.config.package).assess()
                lines += [f"state: {device.state}", f"model: {device.model or '-'}", f"android: {client.shell('getprop', 'ro.build.version.release').strip()}", f"sdk: {client.shell('getprop', 'ro.build.version.sdk').strip()}", f"geometry: {geometry.width}x{geometry.height} density={geometry.density} rotation={geometry.rotation}", f"health: {'safe' if health.safe else 'blocked'} blockers={','.join(health.blockers) or 'none'}"]
            except Exception as exc:
                lines.append(f"error: {exc}")
            return "\n".join(lines)
        def done(output: object) -> None:
            self.diagnostics_output.configure(state="normal")
            self.diagnostics_output.delete("1.0", "end")
            self.diagnostics_output.insert("1.0", str(output))
            self.diagnostics_output.configure(state="disabled")
            self._set_status("Diagnostics completed")
        self._submit(run, done, "Running diagnostics...")

    def _browse_push(self) -> None:
        selected = filedialog.askopenfilename(title="Select file to push")
        if selected:
            self.push_source.set(selected)

    def _browse_pull(self) -> None:
        source = self.pull_source.get().strip()
        folder = filedialog.askdirectory(title="Choose destination folder")
        if folder:
            self.pull_destination.set(str(Path(folder) / (Path(source).name or "pulled_file")))

    def _browse_device_files(self) -> None:
        client = self._gui_client()
        if not client:
            return
        self._start_transfer("Scanning device media...")
        def done(files: object) -> None:
            self.file_progress.stop()
            choices = list(files)
            self.file_status.configure(text=f"Found {len(choices)} files", style="Info.TLabel")
            if not choices:
                self._handle_error("No files found in DCIM/Pictures/Download/Movies/Documents")
                return
            dialog = tk.Toplevel(self)
            dialog.title("Select device file")
            dialog.geometry("980x560")
            dialog.transient(self)
            dialog.minsize(760, 420)
            search = tk.StringVar()
            ttk.Label(dialog, text="Filter by filename or folder").pack(anchor="w", padx=12, pady=(10, 2))
            search_entry = ttk.Entry(dialog, textvariable=search)
            search_entry.pack(fill="x", padx=12, pady=(0, 6))
            content = ttk.Panedwindow(dialog, orient="horizontal")
            content.pack(fill="both", expand=True, padx=12, pady=8)
            list_frame = ttk.Frame(content)
            preview_frame = ttk.LabelFrame(content, text="Preview", padding=10)
            content.add(list_frame, weight=3)
            content.add(preview_frame, weight=2)
            listbox = tk.Listbox(list_frame, font=("Segoe UI", 10), exportselection=False)
            listbox.pack(fill="both", expand=True)
            preview_label = tk.Label(preview_frame, text="Click a file to preview", bg="#101923", fg="#d8e3ef", anchor="center")
            preview_label.pack(fill="both", expand=True)
            preview_info = ttk.Label(preview_frame, text="", style="Subtle.TLabel", wraplength=300)
            preview_info.pack(fill="x", pady=(8, 0))
            preview_state = {"token": 0, "photo": None}
            visible_choices = list(choices)
            def refresh_list(*_args) -> None:
                visible_choices.clear()
                query = search.get().strip().lower()
                visible_choices.extend(path for path in choices if not query or query in path.lower())
                listbox.delete(0, "end")
                for path in visible_choices:
                    listbox.insert("end", path)
            refresh_list()
            search.trace_add("write", refresh_list)
            def preview_selected(_event: object = None) -> None:
                selected = listbox.curselection()
                if not selected:
                    return
                source = visible_choices[selected[0]]
                preview_state["token"] += 1
                token = preview_state["token"]
                suffix = Path(source).suffix.lower()
                preview_label.configure(image="", text="Loading preview...")
                preview_info.configure(text=f"{Path(source).name}\n{source}")
                def load_preview() -> tuple[object, str]:
                    if suffix in {".mp4", ".mkv", ".webm", ".avi", ".mov"}:
                        temp = self.config.artifacts_dir / "preview" / f"{time.time_ns()}_{Path(source).name}"
                        temp.parent.mkdir(parents=True, exist_ok=True)
                        client.pull(source, temp)
                        return temp, suffix
                    return client.pull_bytes(source), suffix
                def show_preview(value: object) -> None:
                    preview_input, extension = value  # type: ignore[misc]
                    temp_to_cleanup = Path(preview_input) if extension in {".mp4", ".mkv", ".webm", ".avi", ".mov"} else None
                    if token != preview_state["token"] or not dialog.winfo_exists():
                        if temp_to_cleanup is not None:
                            temp_to_cleanup.unlink(missing_ok=True)
                        return
                    raw = preview_input if isinstance(preview_input, bytes) else None
                    try:
                        from PIL import Image, ImageTk
                        image = None
                        if extension in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}:
                            if raw is None:
                                raise ValueError("Image preview data is unavailable")
                            image = Image.open(BytesIO(raw)).convert("RGB")
                        elif extension in {".mp4", ".mkv", ".webm", ".avi", ".mov"}:
                            import cv2
                            if temp_to_cleanup is None:
                                raise ValueError("Video preview file is unavailable")
                            temp = temp_to_cleanup
                            capture = cv2.VideoCapture(str(temp))
                            ok, frame = capture.read()
                            fps = capture.get(cv2.CAP_PROP_FPS) or 30
                            frame_count = max(1, int(fps * 3))
                            for _ in range(frame_count - 1):
                                ok, frame = capture.read()
                                if not ok:
                                    break
                            capture.release()
                            if ok:
                                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                                image = Image.fromarray(frame)
                            preview_info.configure(text=f"{Path(source).name}\nVideo preview: first 3 seconds")
                        if image is None:
                            preview_label.configure(image="", text=f"No preview for {extension or 'unknown type'}")
                            return
                        image.thumbnail((520, 420), Image.Resampling.LANCZOS)
                        photo = ImageTk.PhotoImage(image)
                        preview_state["photo"] = photo
                        preview_label.configure(image=photo, text="")
                        preview_info.configure(text=f"{Path(source).name}\n{image.width}x{image.height}  |  {extension}")
                    except Exception as exc:
                        preview_label.configure(image="", text="Preview unavailable")
                        preview_info.configure(text=f"{Path(source).name}\n{exc}")
                    finally:
                        if temp_to_cleanup is not None:
                            temp_to_cleanup.unlink(missing_ok=True)
                self._submit(load_preview, show_preview, "Loading file preview...", lambda message: self._queue(lambda: preview_info.configure(text=f"Preview error: {message}")))
            listbox.bind("<<ListboxSelect>>", preview_selected)
            def choose() -> None:
                selected = listbox.curselection()
                if selected:
                    source = visible_choices[selected[0]]
                    self.pull_source.set(source)
                    self.pull_destination.set(str(Path.home() / "Downloads" / (Path(source).name or "pulled_file")))
                    self.pull_file_info.configure(text=f"Selected: {Path(source).name}  |  {Path(source).suffix.lower() or 'unknown type'}")
                    self.pull_preview_button.configure(state="normal")
                    dialog.destroy()
                    self._set_status("Device file selected; choose a PC destination")
            ttk.Button(dialog, text="Use selected file", style="Primary.TButton", command=choose).pack(anchor="e", padx=12, pady=(0, 12))
        self._submit(lambda: client.list_files(), done, "Scanning device media...", lambda message: (self.file_progress.stop(), self._handle_error(message)))

    def _preview_pull_file(self) -> None:
        client = self._gui_client()
        source = self.pull_source.get().strip()
        if not client or not source or source.endswith("/"):
            self._handle_error("Select a device file before preview")
            return
        self._start_transfer("Loading preview...")
        def preview() -> tuple[object, str]:
            suffix = Path(source).suffix.lower()
            if suffix in {".mp4", ".mkv", ".webm", ".avi", ".mov"}:
                temp = self.config.artifacts_dir / "preview" / f"{time.time_ns()}_{Path(source).name}"
                temp.parent.mkdir(parents=True, exist_ok=True)
                client.pull(source, temp)
                return temp, suffix
            return client.pull_bytes(source), suffix
        def done(value: object) -> None:
            self.file_progress.stop()
            preview_input, suffix = value  # type: ignore[misc]
            if suffix in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}:
                try:
                    from PIL import Image, ImageTk
                    if not isinstance(preview_input, bytes):
                        raise ValueError("Image preview data is unavailable")
                    image = Image.open(BytesIO(preview_input)).convert("RGB")
                    image.thumbnail((720, 720), Image.Resampling.LANCZOS)
                    dialog = tk.Toplevel(self)
                    dialog.title(f"Preview: {Path(source).name}")
                    photo = ImageTk.PhotoImage(image)
                    label = tk.Label(dialog, image=photo, bg="#101923")
                    label.image = photo
                    label.pack(padx=12, pady=12)
                    ttk.Label(dialog, text=f"{Path(source).name}  |  {image.width}x{image.height}").pack(pady=(0, 10))
                    self.file_status.configure(text=f"Image preview loaded: {Path(source).name}", style="Ready.TLabel")
                except Exception as exc:
                    self._transfer_error(f"Cannot preview image: {exc}")
            elif suffix in {".mp4", ".mkv", ".webm", ".avi", ".mov"}:
                temp = Path(preview_input)
                try:
                    import cv2
                    from PIL import Image, ImageTk
                    capture = cv2.VideoCapture(str(temp))
                    ok, frame = capture.read()
                    fps = capture.get(cv2.CAP_PROP_FPS) or 30
                    frame_count = max(1, int(fps * 3))
                    for _ in range(frame_count - 1):
                        ok, frame = capture.read()
                        if not ok:
                            break
                    capture.release()
                    if not ok:
                        raise ValueError("No decodable video frame")
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    image = Image.fromarray(frame)
                    image.thumbnail((720, 720), Image.Resampling.LANCZOS)
                    dialog = tk.Toplevel(self)
                    dialog.title(f"Preview: {Path(source).name}")
                    photo = ImageTk.PhotoImage(image)
                    label = tk.Label(dialog, image=photo, bg="#101923")
                    label.image = photo
                    label.pack(padx=12, pady=12)
                    ttk.Label(dialog, text=f"{Path(source).name}  |  first 3 seconds (sample frame)").pack(pady=(0, 10))
                    self.file_status.configure(text=f"Video preview loaded: first 3 seconds of {Path(source).name}", style="Ready.TLabel")
                except Exception as exc:
                    self._transfer_error(f"Cannot preview video: {exc}")
                finally:
                    temp.unlink(missing_ok=True)
            else:
                self.file_status.configure(text=f"Preview unavailable for {suffix or 'unknown type'}", style="Warning.TLabel")
        self._submit(preview, done, "Loading preview...", self._transfer_error)

    def _browse_apk(self) -> None:
        selected = filedialog.askopenfilename(title="Select APK", filetypes=(("Android APK", "*.apk"), ("All files", "*.*")))
        if selected:
            self.apk_source.set(selected)

    def _transfer_confirm(self, title: str, message: str) -> bool:
        return messagebox.askyesno(title, message, parent=self)

    def _start_transfer(self, label: str) -> bool:
        if not self._current_serial:
            self._handle_error("Select a ready device first")
            return False
        self.file_progress.start(12)
        self.file_status.configure(text=label, style="Info.TLabel")
        self._set_status(label)
        return True

    def _finish_transfer(self, label: str) -> None:
        self.file_progress.stop()
        self._log(label)
        self.file_status.configure(text=label, style="Ready.TLabel")
        self._set_status(label)

    def _transfer_error(self, message: str) -> None:
        self.file_progress.stop()
        self.file_status.configure(text=f"Failed: {message}", style="Error.TLabel")
        self._handle_error(message)

    def _push_file(self) -> None:
        client = self._gui_client()
        source, destination = self.push_source.get().strip(), self.push_destination.get().strip()
        if not client or not source or not destination:
            self._handle_error("Push requires a local file and absolute Android destination")
            return
        if not self._transfer_confirm("Confirm push", f"Push {Path(source).name} to {destination} on {client.serial}?"):
            return
        if not self._start_transfer("Pushing file..."):
            return
        self.file_status.configure(text=f"Pushing {Path(source).name} ({Path(source).stat().st_size:,} bytes)...", style="Info.TLabel")
        def done(result: object) -> None:
            self._finish_transfer(f"PUSH  {source} -> {destination}  rc={result.returncode}")
        self._submit(lambda: client.push(source, destination), done, "Pushing file...", self._transfer_error)

    def _pull_file(self) -> None:
        client = self._gui_client()
        source, destination = self.pull_source.get().strip(), self.pull_destination.get().strip()
        if not client or not source or not destination:
            self._handle_error("Pull requires an absolute Android source and local destination")
            return
        if not self._transfer_confirm("Confirm pull", f"Pull {source} from {client.serial} to {destination}?"):
            return
        if not self._start_transfer("Pulling file..."):
            return
        def done(result: object) -> None:
            digest = AdbClient.sha256_file(destination) if Path(destination).is_file() else "-"
            self._finish_transfer(f"PULL completed  {Path(destination).name}  {Path(destination).stat().st_size:,} bytes  sha256={digest[:16]}...")
        self._submit(lambda: client.pull(source, destination), done, "Pulling file...", self._transfer_error)

    def _install_apk(self) -> None:
        client = self._gui_client()
        apk = self.apk_source.get().strip()
        if not client or not apk:
            self._handle_error("Select an APK and a ready device first")
            return
        if not self._transfer_confirm("Confirm APK install", f"Install {Path(apk).name} on {client.serial}?"):
            return
        if not self._start_transfer("Installing APK..."):
            return
        self.file_status.configure(text=f"Installing {Path(apk).name} ({Path(apk).stat().st_size:,} bytes)...", style="Info.TLabel")
        def done(result: object) -> None:
            self._finish_transfer(f"INSTALL  {apk}  rc={result.returncode}")
        self._submit(lambda: client.install(apk), done, "Installing APK...", self._transfer_error)

    def _set_status(self, text: str) -> None:
        self.status.configure(text=text)

    def _log(self, message: str) -> None:
        severity = "Error" if "ERROR" in message else "Warning" if "WARNING" in message else "Info"
        serial = self._current_serial or "-"
        self._log_entries.append((severity, serial, message))
        self._refresh_logs()

    def _refresh_logs(self) -> None:
        if not hasattr(self, "activity"):
            return
        severity = self.log_severity_var.get()
        device = self.log_device_var.get()
        lines = [f"{message}\n" for level, serial, message in self._log_entries if (severity == "All" or level == severity) and (device == "All" or serial == device)]
        visible_text = "".join(lines)
        artifact_candidates = re.findall(r"[A-Za-z]:\\[^\r\n]+?\.(?:json|png|log)", visible_text)
        self._latest_log_artifact = next((Path(candidate) for candidate in reversed(artifact_candidates) if Path(candidate).exists()), None)
        if hasattr(self, "open_log_artifact_button"):
            self.open_log_artifact_button.configure(state="normal" if self._latest_log_artifact else "disabled")
        self.activity.configure(state="normal")
        self.activity.delete("1.0", "end")
        self.activity.insert("end", visible_text)
        self.activity.see("end")
        self.activity.configure(state="disabled")
        if hasattr(self, "log_device_filter"):
            self.log_device_filter.configure(values=("All", *sorted({serial for _, serial, _ in self._log_entries if serial != "-"})))

    def _handle_error(self, message: str) -> None:
        if self._current_serial:
            self._opening_scrcpy.discard(self._current_serial)
        self.detail_state.configure(text="Action failed", style="Error.TLabel")
        self._log(f"ERROR  {message}")
        self._set_status(message)

    def _on_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._stop_recording(save=False)
        self.scrcpy_manager.stop_all()
        self._write_ui_state()
        self._executor.shutdown(wait=True, cancel_futures=True)
        self.destroy()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Python ADB Controller GUI")
    parser.add_argument("--config", help="Đường dẫn config TOML")
    args = parser.parse_args(argv)
    app = DeviceDashboard(args.config)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
