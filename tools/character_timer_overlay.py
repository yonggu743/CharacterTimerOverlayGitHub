"""Character skill cooldown and buff timer overlay.

This tool watches a fixed party-list screen region, recognizes character names,
listens for skill keys, and displays cooldown timers next to the recognized
character positions.

Example:
  python tools/character_timer_overlay.py --region 2175,288,376,480
"""

from __future__ import annotations

import argparse
import ctypes
import difflib
import json
import queue
import re
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass, replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

import cv2
import keyboard
import numpy as np
from rapidocr_onnxruntime import RapidOCR

from screen_reader import CaptureRegion, ScreenReader, select_region


HWND_TOPMOST = -1
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040
DEFAULT_REGION = CaptureRegion(left=2175, top=288, width=376, height=480)
NOISE_RE = re.compile(r"[\s0-9A-Za-z_.,:;!?\-+/\\|()\[\]{}<>]")
KEY_DEBOUNCE_SECONDS = 0.12
PARTY_HOLD_SECONDS = 30.0
PARTY_SLOT_COUNT = 4
PARTY_SWITCH_KEYS = {"1", "2", "3", "4"}
DEFAULT_OCR_INTERVAL_SECONDS = 5.0
DEFAULT_CHANGE_CONFIRM_FRAMES = 3
DEFAULT_CHANGE_CONFIRM_INTERVAL_SECONDS = 0.25
CD_LABEL_LEFT_OFFSET = 88
BUFF_PANEL_LEFT_OFFSET = 140
BUFF_PANEL_TOP_OFFSET = 78


DEFAULT_SETTINGS: dict[str, Any] = {
    "min_match_score": 0.58,
    "ocr_interval": DEFAULT_OCR_INTERVAL_SECONDS,
    "change_confirm_frames": DEFAULT_CHANGE_CONFIRM_FRAMES,
    "change_confirm_interval": DEFAULT_CHANGE_CONFIRM_INTERVAL_SECONDS,
    "party_hold_seconds": PARTY_HOLD_SECONDS,
    "key_debounce_seconds": KEY_DEBOUNCE_SECONDS,
    "cd_label_left_offset": CD_LABEL_LEFT_OFFSET,
    "cd_label_font_size": 15,
    "cd_label_alpha": 0.86,
    "buff_panel_left_offset": BUFF_PANEL_LEFT_OFFSET,
    "buff_panel_top_offset": BUFF_PANEL_TOP_OFFSET,
    "buff_panel_font_size": 14,
    "buff_panel_alpha": 0.88,
}


@dataclass(frozen=True)
class CharacterConfig:
    name: str
    aliases: tuple[str, ...]
    skill_key: str
    skill_cd: float
    buff_key: str
    buff_duration: float
    buff_label: str


@dataclass(frozen=True)
class OverlaySettings:
    min_match_score: float
    ocr_interval: float
    change_confirm_frames: int
    change_confirm_interval: float
    party_hold_seconds: float
    key_debounce_seconds: float
    cd_label_left_offset: int
    cd_label_font_size: int
    cd_label_alpha: float
    buff_panel_left_offset: int
    buff_panel_top_offset: int
    buff_panel_font_size: int
    buff_panel_alpha: float


@dataclass
class TimerState:
    name: str
    label: str
    end_at: float
    duration: float


@dataclass(frozen=True)
class RecognizedCharacter:
    name: str
    confidence: float
    slot: int
    x: int
    y: int
    width: int
    height: int


def normalize_name(text: str) -> str:
    return NOISE_RE.sub("", text).strip()


def load_database(path: Path) -> tuple[dict[str, CharacterConfig], CaptureRegion, OverlaySettings]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    region = CaptureRegion.parse(data.get("default_region", "2175,288,376,480"))
    settings = load_settings(data.get("settings", {}))
    characters: dict[str, CharacterConfig] = {}
    for name, raw in data["characters"].items():
        aliases = tuple(raw.get("aliases", [name]))
        characters[name] = CharacterConfig(
            name=name,
            aliases=aliases,
            skill_key=str(raw.get("skill_key", "e")).lower(),
            skill_cd=float(raw.get("skill_cd", 10.0)),
            buff_key=str(raw.get("buff_key", "")).lower(),
            buff_duration=float(raw.get("buff_duration", 0.0)),
            buff_label=str(raw.get("buff_label", "")),
        )
    return characters, region, settings


def load_settings(raw: dict[str, Any]) -> OverlaySettings:
    data = {**DEFAULT_SETTINGS, **(raw or {})}
    return OverlaySettings(
        min_match_score=float(data["min_match_score"]),
        ocr_interval=max(0.5, float(data["ocr_interval"])),
        change_confirm_frames=max(1, int(data["change_confirm_frames"])),
        change_confirm_interval=max(0.05, float(data["change_confirm_interval"])),
        party_hold_seconds=max(1.0, float(data["party_hold_seconds"])),
        key_debounce_seconds=max(0.0, float(data["key_debounce_seconds"])),
        cd_label_left_offset=max(0, int(data["cd_label_left_offset"])),
        cd_label_font_size=max(8, int(data["cd_label_font_size"])),
        cd_label_alpha=min(1.0, max(0.2, float(data["cd_label_alpha"]))),
        buff_panel_left_offset=max(0, int(data["buff_panel_left_offset"])),
        buff_panel_top_offset=max(0, int(data["buff_panel_top_offset"])),
        buff_panel_font_size=max(8, int(data["buff_panel_font_size"])),
        buff_panel_alpha=min(1.0, max(0.2, float(data["buff_panel_alpha"]))),
    )


def settings_to_dict(settings: OverlaySettings) -> dict[str, Any]:
    return {
        "min_match_score": settings.min_match_score,
        "ocr_interval": settings.ocr_interval,
        "change_confirm_frames": settings.change_confirm_frames,
        "change_confirm_interval": settings.change_confirm_interval,
        "party_hold_seconds": settings.party_hold_seconds,
        "key_debounce_seconds": settings.key_debounce_seconds,
        "cd_label_left_offset": settings.cd_label_left_offset,
        "cd_label_font_size": settings.cd_label_font_size,
        "cd_label_alpha": settings.cd_label_alpha,
        "buff_panel_left_offset": settings.buff_panel_left_offset,
        "buff_panel_top_offset": settings.buff_panel_top_offset,
        "buff_panel_font_size": settings.buff_panel_font_size,
        "buff_panel_alpha": settings.buff_panel_alpha,
    }


def save_runtime_config(path: Path, region: CaptureRegion, settings: OverlaySettings) -> None:
    data: dict[str, Any]
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    else:
        data = {"characters": {}}
    data["default_region"] = f"{region.left},{region.top},{region.width},{region.height}"
    data["settings"] = settings_to_dict(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_raw_database(path: Path) -> dict[str, Any]:
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    else:
        data = {"default_region": "2175,288,376,480", "settings": {}, "characters": {}}
    data.setdefault("default_region", "2175,288,376,480")
    settings = data.setdefault("settings", {})
    for key, value in DEFAULT_SETTINGS.items():
        settings.setdefault(key, value)
    data.setdefault("characters", {})
    return data


def save_raw_database(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def default_db_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).with_name("character_timers_db.json")
    return Path(__file__).with_name("character_timers_db.json")


def is_user_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


class PartyOcr:
    def __init__(self, characters: dict[str, CharacterConfig], min_score: float) -> None:
        self.ocr = RapidOCR()
        self.characters = characters
        self.min_score = min_score
        self.alias_index: list[tuple[str, str]] = []
        for name, config in characters.items():
            for alias in config.aliases:
                self.alias_index.append((name, normalize_name(alias)))

    def read_party(self, frame: np.ndarray, region: CaptureRegion) -> list[RecognizedCharacter]:
        prepared, scale = self._prepare(frame)
        result, _elapsed = self.ocr(prepared)
        if not result:
            return []

        candidates: list[RecognizedCharacter] = []
        for box, text, confidence in result:
            if confidence < 0.25:
                continue
            match = self._match_name(text)
            if match is None:
                continue
            name, score = match
            if score < self.min_score:
                continue
            xs = [point[0] / scale for point in box]
            ys = [point[1] / scale for point in box]
            left = int(region.left + min(xs))
            top = int(region.top + min(ys))
            right = int(region.left + max(xs))
            bottom = int(region.top + max(ys))
            candidates.append(
                RecognizedCharacter(
                    name=name,
                    confidence=score,
                    slot=self._slot_for_box(min(ys), max(ys), region),
                    x=left,
                    y=top,
                    width=max(1, right - left),
                    height=max(1, bottom - top),
                )
            )

        return self._dedupe(candidates)

    def _match_name(self, text: str) -> tuple[str, float] | None:
        cleaned = normalize_name(text)
        if not cleaned:
            return None
        best_name = ""
        best_score = 0.0
        for name, alias in self.alias_index:
            if not alias:
                continue
            if alias in cleaned or cleaned in alias:
                score = min(len(cleaned), len(alias)) / max(len(cleaned), len(alias))
            else:
                score = difflib.SequenceMatcher(a=cleaned, b=alias, autojunk=False).ratio()
            if score > best_score:
                best_name = name
                best_score = score
        if not best_name:
            return None
        return best_name, best_score

    @staticmethod
    def _prepare(frame: np.ndarray) -> tuple[np.ndarray, float]:
        scale = 2.4
        enlarged = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        gray = cv2.cvtColor(enlarged, cv2.COLOR_BGR2GRAY)
        gray = cv2.convertScaleAbs(gray, alpha=1.45, beta=8)
        gray = cv2.bilateralFilter(gray, 5, 45, 45)
        prepared = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        return prepared, scale

    @staticmethod
    def _dedupe(candidates: list[RecognizedCharacter]) -> list[RecognizedCharacter]:
        best: dict[str, RecognizedCharacter] = {}
        for item in candidates:
            previous = best.get(item.name)
            if previous is None or item.confidence > previous.confidence:
                best[item.name] = item
        return sorted(best.values(), key=lambda item: item.y)

    @staticmethod
    def _slot_for_box(top: float, bottom: float, region: CaptureRegion) -> int:
        center_y = (top + bottom) / 2.0
        relative_y = max(0.0, center_y)
        slot_height = region.height / PARTY_SLOT_COUNT
        slot = int(relative_y // slot_height) + 1
        return max(1, min(PARTY_SLOT_COUNT, slot))


class OverlayLabel:
    def __init__(
        self,
        root: tk.Tk,
        fg: str,
        bg: str,
        font: tuple[str, int, str],
        alpha: float,
    ) -> None:
        self.fg = fg
        self.font = font
        self.alpha = alpha
        self.transparent = "#ff00ff"
        self.width = 78
        self.height = 34
        self.window = tk.Toplevel(root)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.attributes("-alpha", alpha)
        self.window.configure(bg=self.transparent)
        try:
            self.window.attributes("-transparentcolor", self.transparent)
        except Exception:
            pass
        self.canvas = tk.Canvas(
            self.window,
            width=self.width,
            height=self.height,
            bg=self.transparent,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack()
        self.hide()

    def configure_style(self, font_size: int, alpha: float) -> None:
        self.font = ("Segoe UI", font_size, "bold")
        self.alpha = alpha
        self.window.attributes("-alpha", alpha)

    def show(self, text: str, x: int, y: int, progress: float = 1.0) -> None:
        progress = min(1.0, max(0.0, progress))
        self._draw(text, progress)
        self.window.deiconify()
        self.window.geometry(f"+{x}+{y}")
        self.window.update_idletasks()
        self._force_topmost()

    def hide(self) -> None:
        self.window.withdraw()

    def destroy(self) -> None:
        self.window.destroy()

    def _draw(self, text: str, progress: float) -> None:
        self.canvas.delete("all")
        w = self.width
        h = self.height
        self._round_rect(1, 1, w - 1, h - 1, 10, fill="#101720", outline="#76fff1", width=1)
        self._round_rect(3, 3, w - 3, h - 3, 8, fill="#172330", outline="#27475a", width=1)
        self.canvas.create_oval(9, 12, 17, 20, fill="#53fff0", outline="")
        self.canvas.create_text(
            47,
            15,
            text=text,
            fill="#66fff4",
            font=self.font,
            anchor=tk.CENTER,
        )
        bar_x = 12
        bar_y = h - 8
        bar_w = w - 24
        self._round_rect(bar_x, bar_y, bar_x + bar_w, bar_y + 3, 2, fill="#2b3944", outline="")
        fill_w = max(3, int(bar_w * progress))
        color = "#65fff1" if progress > 0.28 else "#ffcf5a"
        self._round_rect(bar_x, bar_y, bar_x + fill_w, bar_y + 3, 2, fill=color, outline="")

    def _round_rect(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        radius: int,
        **kwargs: Any,
    ) -> None:
        points = [
            x1 + radius, y1,
            x2 - radius, y1,
            x2, y1,
            x2, y1 + radius,
            x2, y2 - radius,
            x2, y2,
            x2 - radius, y2,
            x1 + radius, y2,
            x1, y2,
            x1, y2 - radius,
            x1, y1 + radius,
            x1, y1,
        ]
        self.canvas.create_polygon(points, smooth=True, **kwargs)

    def _force_topmost(self) -> None:
        self.window.attributes("-topmost", True)
        try:
            ctypes.windll.user32.SetWindowPos(
                self.window.winfo_id(),
                HWND_TOPMOST,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
            )
        except Exception:
            pass


class BuffPanel:
    def __init__(self, root: tk.Tk, settings: OverlaySettings) -> None:
        self.settings = settings
        self.transparent = "#ff00ff"
        self.window = tk.Toplevel(root)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.attributes("-alpha", settings.buff_panel_alpha)
        self.window.configure(bg=self.transparent)
        try:
            self.window.attributes("-transparentcolor", self.transparent)
        except Exception:
            pass
        self.canvas = tk.Canvas(
            self.window,
            width=178,
            height=52,
            bg=self.transparent,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack()
        self.hide()

    def show(self, rows: list[tuple[str, float, float]], x: int, y: int) -> None:
        self._draw(rows)
        self.window.deiconify()
        self.window.geometry(f"+{x}+{y}")
        self.window.update_idletasks()
        self._force_topmost()

    def hide(self) -> None:
        self.window.withdraw()

    def destroy(self) -> None:
        self.window.destroy()

    def _draw(self, rows: list[tuple[str, float, float]]) -> None:
        row_h = 28
        width = 178
        height = 14 + row_h * len(rows)
        self.canvas.configure(width=width, height=height)
        self.canvas.delete("all")
        self._round_rect(1, 1, width - 1, height - 1, 12, fill="#111620", outline="#f3d46b", width=1)
        self._round_rect(4, 4, width - 4, height - 4, 10, fill="#1b2230", outline="#3c3440", width=1)
        self.canvas.create_text(
            14,
            12,
            text="BUFF",
            fill="#f8dd7c",
            font=("Segoe UI", 8, "bold"),
            anchor=tk.W,
        )
        for index, (label, remaining, progress) in enumerate(rows):
            progress = min(1.0, max(0.0, progress))
            top = 20 + index * row_h
            self.canvas.create_text(
                14,
                top + 10,
                text=label,
                fill="#fff3bd",
                font=("Microsoft YaHei UI", self.settings.buff_panel_font_size, "bold"),
                anchor=tk.W,
            )
            self.canvas.create_text(
                width - 14,
                top + 10,
                text=f"{remaining:04.1f}",
                fill="#66fff4",
                font=("Segoe UI", self.settings.buff_panel_font_size, "bold"),
                anchor=tk.E,
            )
            bar_x = 14
            bar_y = top + 21
            bar_w = width - 28
            self._round_rect(bar_x, bar_y, bar_x + bar_w, bar_y + 3, 2, fill="#303640", outline="")
            self._round_rect(
                bar_x,
                bar_y,
                bar_x + max(4, int(bar_w * progress)),
                bar_y + 3,
                2,
                fill="#f8d86d",
                outline="",
            )

    def _round_rect(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        radius: int,
        **kwargs: Any,
    ) -> None:
        points = [
            x1 + radius, y1,
            x2 - radius, y1,
            x2, y1,
            x2, y1 + radius,
            x2, y2 - radius,
            x2, y2,
            x2 - radius, y2,
            x1 + radius, y2,
            x1, y2,
            x1, y2 - radius,
            x1, y1 + radius,
            x1, y1,
        ]
        self.canvas.create_polygon(points, smooth=True, **kwargs)

    def _force_topmost(self) -> None:
        self.window.attributes("-topmost", True)
        try:
            ctypes.windll.user32.SetWindowPos(
                self.window.winfo_id(),
                HWND_TOPMOST,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
            )
        except Exception:
            pass


class TimerOverlayApp(tk.Tk):
    def __init__(
        self,
        db_path: Path,
        characters: dict[str, CharacterConfig],
        region: CaptureRegion,
        fps: float,
        settings: OverlaySettings,
        debug: bool,
        auto_start: bool,
    ) -> None:
        super().__init__()
        self.title("角色技能 CD / Buff 悬浮计时器")
        self.geometry("760x520")
        self.minsize(700, 480)

        self.db_path = db_path
        self.characters = characters
        self.region = region
        self.fps = fps
        self.settings = settings
        self.debug = debug
        self.events: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self.stop_event = threading.Event()
        self.ocr_stop_event: threading.Event | None = None
        self.ocr_thread: threading.Thread | None = None
        self.positions: dict[str, RecognizedCharacter] = {}
        self.slot_characters: dict[int, str] = {}
        self.active_slot: int | None = None
        self.active_character: str | None = None
        self.last_party_at = 0.0
        self.cooldowns: dict[str, TimerState] = {}
        self.buffs: dict[str, TimerState] = {}
        self.cd_labels: dict[str, OverlayLabel] = {}
        self.keyboard_hook = None
        self.last_key_at: dict[str, float] = {}
        self.buff_panel = BuffPanel(self, settings)

        self.running = False
        self.ocr_enabled_var = tk.BooleanVar(value=True)
        self.keyboard_enabled_var = tk.BooleanVar(value=True)
        self.overlay_enabled_var = tk.BooleanVar(value=True)
        self.db = load_raw_database(self.db_path)
        self.selected_name: str | None = None
        self.region_var = tk.StringVar(
            value=f"{region.left},{region.top},{region.width},{region.height}"
        )
        self.fps_var = tk.StringVar(value=str(fps))
        self.min_match_score_var = tk.StringVar(value=str(settings.min_match_score))
        self.ocr_interval_var = tk.StringVar(value=str(settings.ocr_interval))
        self.change_confirm_frames_var = tk.StringVar(value=str(settings.change_confirm_frames))
        self.change_confirm_interval_var = tk.StringVar(value=str(settings.change_confirm_interval))
        self.party_hold_seconds_var = tk.StringVar(value=str(settings.party_hold_seconds))
        self.key_debounce_seconds_var = tk.StringVar(value=str(settings.key_debounce_seconds))
        self.cd_label_left_offset_var = tk.StringVar(value=str(settings.cd_label_left_offset))
        self.cd_label_font_size_var = tk.StringVar(value=str(settings.cd_label_font_size))
        self.cd_label_alpha_var = tk.StringVar(value=str(settings.cd_label_alpha))
        self.buff_panel_left_offset_var = tk.StringVar(value=str(settings.buff_panel_left_offset))
        self.buff_panel_top_offset_var = tk.StringVar(value=str(settings.buff_panel_top_offset))
        self.buff_panel_font_size_var = tk.StringVar(value=str(settings.buff_panel_font_size))
        self.buff_panel_alpha_var = tk.StringVar(value=str(settings.buff_panel_alpha))
        self.status_var = tk.StringVar(value="状态: 已停止")
        self.listener_var = tk.StringVar(value="键盘监听: 未启动")
        self.ocr_var = tk.StringVar(value="OCR: 未启动")
        self.active_var = tk.StringVar(value="当前角色: -")
        self.party_var = tk.StringVar(value="队伍: -")
        self.timer_var = tk.StringVar(value="计时: -")
        self.db_var = tk.StringVar(value=f"数据库: {self.db_path}")
        self.db_status_var = tk.StringVar(value=f"数据库: {self.db_path}")
        self.name_var = tk.StringVar()
        self.aliases_var = tk.StringVar()
        self.skill_key_var = tk.StringVar(value="e")
        self.skill_cd_var = tk.StringVar(value="10.0")
        self.buff_key_var = tk.StringVar(value="")
        self.buff_duration_var = tk.StringVar(value="0.0")
        self.buff_label_var = tk.StringVar()
        self.character_search_var = tk.StringVar()
        self.character_search_var.trace_add("write", lambda *_args: self._refresh_character_list())

        self._build_control_panel()
        self._refresh_character_list()
        self.bind("<Escape>", lambda _event: self.destroy())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.after(50, self._tick)
        if auto_start:
            self.after(100, self.start_runtime)

    def destroy(self) -> None:
        self.stop_runtime()
        self.stop_event.set()
        for label in self.cd_labels.values():
            label.destroy()
        self.buff_panel.destroy()
        super().destroy()

    def _build_control_panel(self) -> None:
        outer = ttk.Frame(self, padding=8)
        outer.pack(fill=tk.BOTH, expand=True)
        notebook = ttk.Notebook(outer)
        notebook.pack(fill=tk.BOTH, expand=True)
        root = ttk.Frame(notebook, padding=12)
        database_root = ttk.Frame(notebook, padding=12)
        notebook.add(root, text="运行控制")
        notebook.add(database_root, text="角色数据库")

        header = ttk.Frame(root)
        header.pack(fill=tk.X)
        ttk.Label(
            header,
            text="角色技能 CD / Buff 悬浮计时器",
            font=("Microsoft YaHei UI", 16, "bold"),
        ).pack(side=tk.LEFT)
        ttk.Button(header, text="启动", command=self.start_runtime).pack(side=tk.RIGHT)
        ttk.Button(header, text="停止", command=self.stop_runtime).pack(side=tk.RIGHT, padx=(0, 8))

        status_frame = ttk.LabelFrame(root, text="运行状态", padding=10)
        status_frame.pack(fill=tk.X, pady=(12, 8))
        for variable in (
            self.status_var,
            self.listener_var,
            self.ocr_var,
            self.active_var,
            self.party_var,
            self.timer_var,
            self.db_var,
        ):
            ttk.Label(status_frame, textvariable=variable, anchor=tk.W).pack(fill=tk.X)

        switches = ttk.LabelFrame(root, text="功能开关", padding=10)
        switches.pack(fill=tk.X, pady=(0, 8))
        ttk.Checkbutton(
            switches,
            text="开启 OCR 识别",
            variable=self.ocr_enabled_var,
            command=self._toggle_ocr_runtime,
        ).pack(side=tk.LEFT, padx=(0, 18))
        ttk.Checkbutton(
            switches,
            text="开启键盘监听",
            variable=self.keyboard_enabled_var,
            command=self._toggle_keyboard_runtime,
        ).pack(side=tk.LEFT, padx=(0, 18))
        ttk.Checkbutton(
            switches,
            text="显示悬浮倒计时",
            variable=self.overlay_enabled_var,
            command=self._toggle_overlay_runtime,
        ).pack(side=tk.LEFT)

        settings_frame = ttk.LabelFrame(root, text="运行参数", padding=10)
        settings_frame.pack(fill=tk.BOTH, expand=True)
        for column in (1, 3):
            settings_frame.columnconfigure(column, weight=1)

        ttk.Label(settings_frame, text="识别区域").grid(row=0, column=0, sticky=tk.W, pady=4)
        ttk.Entry(settings_frame, textvariable=self.region_var, width=28, state="readonly").grid(
            row=0,
            column=1,
            sticky=tk.EW,
            padx=(8, 18),
            pady=4,
        )
        ttk.Button(
            settings_frame,
            text="框选识别区域",
            command=self.select_capture_region,
        ).grid(row=0, column=2, columnspan=2, sticky=tk.W, pady=4)
        self._entry(settings_frame, "截屏 FPS", self.fps_var, 1, 0)
        self._entry(settings_frame, "OCR 间隔 秒", self.ocr_interval_var, 1, 2)
        self._entry(settings_frame, "匹配阈值", self.min_match_score_var, 2, 0)
        self._entry(settings_frame, "变更确认帧数", self.change_confirm_frames_var, 2, 2)
        self._entry(settings_frame, "确认间隔 秒", self.change_confirm_interval_var, 3, 0)
        self._entry(settings_frame, "空识别保留 秒", self.party_hold_seconds_var, 3, 2)
        self._entry(settings_frame, "按键防抖 秒", self.key_debounce_seconds_var, 4, 0)
        self._entry(settings_frame, "CD 左移像素", self.cd_label_left_offset_var, 4, 2)
        self._entry(settings_frame, "CD 字号", self.cd_label_font_size_var, 5, 0)
        self._entry(settings_frame, "CD 透明度", self.cd_label_alpha_var, 5, 2)
        self._entry(settings_frame, "Buff 左移像素", self.buff_panel_left_offset_var, 6, 0)
        self._entry(settings_frame, "Buff 上移像素", self.buff_panel_top_offset_var, 6, 2)
        self._entry(settings_frame, "Buff 字号", self.buff_panel_font_size_var, 7, 0)
        self._entry(settings_frame, "Buff 透明度", self.buff_panel_alpha_var, 7, 2)

        actions = ttk.Frame(root)
        actions.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(actions, text="应用参数", command=self.apply_runtime_settings).pack(side=tk.LEFT)
        ttk.Button(actions, text="保存参数到 JSON", command=self.save_runtime_settings).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        ttk.Button(actions, text="重载数据库", command=self.reload_database).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        ttk.Label(
            actions,
            text="提示: 改 OCR/FPS/区域后，运行中会自动重启 OCR 线程。",
            foreground="#555555",
        ).pack(side=tk.RIGHT)
        self._build_database_tab(database_root)

    def _build_database_tab(self, root: ttk.Frame) -> None:
        header = ttk.Frame(root)
        header.pack(fill=tk.X)
        ttk.Label(
            header,
            text="角色技能 CD / Buff 数据库",
            font=("Microsoft YaHei UI", 16, "bold"),
        ).pack(side=tk.LEFT)
        ttk.Button(header, text="打开 JSON", command=self._open_json).pack(side=tk.RIGHT)
        ttk.Button(header, text="另存为", command=self._save_as).pack(side=tk.RIGHT, padx=(0, 8))

        body = ttk.Frame(root)
        body.pack(fill=tk.BOTH, expand=True, pady=(12, 0))

        list_frame = ttk.LabelFrame(body, text="角色列表", padding=8)
        list_frame.pack(side=tk.LEFT, fill=tk.BOTH)
        search_row = ttk.Frame(list_frame)
        search_row.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(search_row, text="搜索").pack(side=tk.LEFT)
        ttk.Entry(search_row, textvariable=self.character_search_var, width=18).pack(
            side=tk.LEFT,
            fill=tk.X,
            expand=True,
            padx=(8, 0),
        )
        self.character_list = tk.Listbox(list_frame, width=24, height=18, exportselection=False)
        self.character_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar = ttk.Scrollbar(list_frame, command=self.character_list.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.character_list.configure(yscrollcommand=scrollbar.set)
        self.character_list.bind("<<ListboxSelect>>", self._on_character_select)

        form = ttk.LabelFrame(body, text="角色配置", padding=14)
        form.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(12, 0))
        self._form_entry(form, "角色名", self.name_var, 0)
        self._form_entry(form, "别名，逗号分隔", self.aliases_var, 1)
        self._form_entry(form, "技能按键", self.skill_key_var, 2)
        self._form_entry(form, "技能 CD 秒数", self.skill_cd_var, 3)
        self._form_entry(form, "Buff 按键", self.buff_key_var, 4)
        self._form_entry(form, "Buff 持续秒数", self.buff_duration_var, 5)
        self._form_entry(form, "Buff 显示名称", self.buff_label_var, 6)

        hint = ttk.Label(
            form,
            text="说明：技能按键和 Buff 按键可以相同；Buff 持续秒数为 0 时不显示 Buff。",
            foreground="#555555",
        )
        hint.grid(row=7, column=0, columnspan=2, sticky=tk.W, pady=(8, 12))

        actions = ttk.Frame(form)
        actions.grid(row=8, column=0, columnspan=2, sticky=tk.W)
        ttk.Button(actions, text="新增", command=self._new_character).pack(side=tk.LEFT)
        ttk.Button(actions, text="保存/更新", command=self._save_character).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        ttk.Button(actions, text="删除", command=self._delete_character).pack(
            side=tk.LEFT, padx=(8, 0)
        )

        preview = ttk.LabelFrame(form, text="当前 JSON 预览", padding=8)
        preview.grid(row=9, column=0, columnspan=2, sticky=tk.NSEW, pady=(14, 0))
        form.rowconfigure(9, weight=1)
        form.columnconfigure(1, weight=1)
        self.preview_text = tk.Text(preview, height=10, wrap=tk.NONE, state=tk.DISABLED)
        self.preview_text.pack(fill=tk.BOTH, expand=True)

        ttk.Label(root, textvariable=self.db_status_var, anchor=tk.W).pack(
            fill=tk.X, pady=(10, 0)
        )

    @staticmethod
    def _form_entry(parent: tk.Widget, label: str, variable: tk.StringVar, row: int) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky=tk.W, pady=5)
        ttk.Entry(parent, textvariable=variable, width=34).grid(
            row=row,
            column=1,
            sticky=tk.EW,
            pady=5,
            padx=(10, 0),
        )

    @staticmethod
    def _entry(
        parent: tk.Widget,
        label: str,
        variable: tk.StringVar,
        row: int,
        column: int,
        width: int = 16,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=column, sticky=tk.W, pady=4)
        ttk.Entry(parent, textvariable=variable, width=width).grid(
            row=row,
            column=column + 1,
            sticky=tk.EW,
            padx=(8, 18),
            pady=4,
        )

    def select_capture_region(self) -> None:
        was_running = self.running
        if was_running:
            self.stop_runtime()
        self._hide_all_overlays()
        messagebox.showinfo(
            "框选识别区域",
            "接下来会打开截图选区窗口。\n"
            "用鼠标拖拽框选角色名区域，按 Enter 或空格确认，按 Esc 取消。",
        )
        selected_region: CaptureRegion | None = None
        try:
            self.withdraw()
            self.update_idletasks()
            time.sleep(0.2)
            selected_region = select_region(monitor=0)
        except Exception as exc:
            messagebox.showerror("框选失败", str(exc))
        finally:
            self.deiconify()
            self.lift()
            self.attributes("-topmost", True)
            self.after(300, lambda: self.attributes("-topmost", False))
        if selected_region is None:
            if was_running:
                self.start_runtime()
            return
        region = selected_region
        self.region_var.set(f"{region.left},{region.top},{region.width},{region.height}")
        if self.apply_runtime_settings(show_success=False):
            self._set_status("状态: 识别区域已更新")
        if was_running:
            self.start_runtime()

    def start_runtime(self) -> None:
        if not self.apply_runtime_settings(show_success=False):
            return
        if self.running:
            return
        self.running = True
        self.stop_event = threading.Event()
        self.last_key_at.clear()
        if self.keyboard_enabled_var.get():
            self._start_keyboard_listener()
        if self.ocr_enabled_var.get():
            self._start_ocr_thread()
        self._set_status("状态: 运行中")
        if self.debug:
            print("runtime started")

    def stop_runtime(self) -> None:
        if not getattr(self, "running", False):
            self._stop_keyboard_listener()
            self._stop_ocr_thread()
            self._hide_all_overlays()
            return
        self.running = False
        self.stop_event.set()
        self._stop_keyboard_listener()
        self._stop_ocr_thread()
        self._hide_all_overlays()
        self._set_status("状态: 已停止")
        if self.debug:
            print("runtime stopped")

    def apply_runtime_settings(self, show_success: bool = True) -> bool:
        try:
            old_region = self.region
            old_fps = self.fps
            new_region = CaptureRegion.parse(self.region_var.get())
            new_fps = self._float_setting(self.fps_var, "截屏 FPS", 0.2, None)
            raw_settings = {
                "min_match_score": self._float_setting(
                    self.min_match_score_var, "匹配阈值", 0.0, 1.0
                ),
                "ocr_interval": self._float_setting(self.ocr_interval_var, "OCR 间隔", 0.5, None),
                "change_confirm_frames": self._int_setting(
                    self.change_confirm_frames_var, "变更确认帧数", 1, None
                ),
                "change_confirm_interval": self._float_setting(
                    self.change_confirm_interval_var, "确认间隔", 0.05, None
                ),
                "party_hold_seconds": self._float_setting(
                    self.party_hold_seconds_var, "空识别保留", 1.0, None
                ),
                "key_debounce_seconds": self._float_setting(
                    self.key_debounce_seconds_var, "按键防抖", 0.0, None
                ),
                "cd_label_left_offset": self._int_setting(
                    self.cd_label_left_offset_var, "CD 左移像素", 0, None
                ),
                "cd_label_font_size": self._int_setting(
                    self.cd_label_font_size_var, "CD 字号", 8, None
                ),
                "cd_label_alpha": self._float_setting(self.cd_label_alpha_var, "CD 透明度", 0.2, 1.0),
                "buff_panel_left_offset": self._int_setting(
                    self.buff_panel_left_offset_var, "Buff 左移像素", 0, None
                ),
                "buff_panel_top_offset": self._int_setting(
                    self.buff_panel_top_offset_var, "Buff 上移像素", 0, None
                ),
                "buff_panel_font_size": self._int_setting(
                    self.buff_panel_font_size_var, "Buff 字号", 8, None
                ),
                "buff_panel_alpha": self._float_setting(
                    self.buff_panel_alpha_var, "Buff 透明度", 0.2, 1.0
                ),
            }
            old_settings = self.settings
            self.region = new_region
            self.fps = new_fps
            self.settings = load_settings(raw_settings)
            self.buff_panel.settings = self.settings
            self.buff_panel.window.attributes("-alpha", self.settings.buff_panel_alpha)
            for label in self.cd_labels.values():
                label.configure_style(
                    self.settings.cd_label_font_size,
                    self.settings.cd_label_alpha,
                )
            if (
                self.running
                and self.ocr_enabled_var.get()
                and (
                    old_region != self.region
                    or old_fps != self.fps
                    or old_settings.min_match_score != self.settings.min_match_score
                    or old_settings.ocr_interval != self.settings.ocr_interval
                    or old_settings.change_confirm_frames != self.settings.change_confirm_frames
                    or old_settings.change_confirm_interval != self.settings.change_confirm_interval
                )
            ):
                self._restart_ocr_thread()
        except Exception as exc:
            messagebox.showerror("参数错误", str(exc))
            return False
        if show_success:
            self._set_status("状态: 参数已应用")
        return True

    def save_runtime_settings(self) -> None:
        if not self.apply_runtime_settings(show_success=False):
            return
        try:
            save_runtime_config(self.db_path, self.region, self.settings)
            self.db = load_raw_database(self.db_path)
            self._update_json_preview()
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        self._set_status("状态: 参数已保存")
        messagebox.showinfo("保存成功", f"已保存到:\n{self.db_path}")

    def reload_database(self) -> None:
        was_running = self.running
        if was_running:
            self.stop_runtime()
        try:
            self.characters, self.region, self.settings = load_database(self.db_path)
            self.db = load_raw_database(self.db_path)
        except Exception as exc:
            messagebox.showerror("重载失败", str(exc))
            return
        self.region_var.set(f"{self.region.left},{self.region.top},{self.region.width},{self.region.height}")
        self._sync_settings_vars()
        self._refresh_character_list()
        self.positions.clear()
        self.slot_characters.clear()
        self.cooldowns.clear()
        self.buffs.clear()
        self.active_slot = None
        self.active_character = None
        self._set_status("状态: 数据库已重载")
        if was_running:
            self.start_runtime()

    def _sync_settings_vars(self) -> None:
        self.min_match_score_var.set(str(self.settings.min_match_score))
        self.ocr_interval_var.set(str(self.settings.ocr_interval))
        self.change_confirm_frames_var.set(str(self.settings.change_confirm_frames))
        self.change_confirm_interval_var.set(str(self.settings.change_confirm_interval))
        self.party_hold_seconds_var.set(str(self.settings.party_hold_seconds))
        self.key_debounce_seconds_var.set(str(self.settings.key_debounce_seconds))
        self.cd_label_left_offset_var.set(str(self.settings.cd_label_left_offset))
        self.cd_label_font_size_var.set(str(self.settings.cd_label_font_size))
        self.cd_label_alpha_var.set(str(self.settings.cd_label_alpha))
        self.buff_panel_left_offset_var.set(str(self.settings.buff_panel_left_offset))
        self.buff_panel_top_offset_var.set(str(self.settings.buff_panel_top_offset))
        self.buff_panel_font_size_var.set(str(self.settings.buff_panel_font_size))
        self.buff_panel_alpha_var.set(str(self.settings.buff_panel_alpha))

    def _refresh_character_list(self) -> None:
        if not hasattr(self, "character_list"):
            return
        query = normalize_name(self.character_search_var.get()).lower()
        names = []
        for name, raw in self.db.get("characters", {}).items():
            haystack = [name, *raw.get("aliases", [])]
            haystack_text = normalize_name(" ".join(str(item) for item in haystack)).lower()
            if not query or query in haystack_text:
                names.append(name)
        names.sort()
        self.character_list.delete(0, tk.END)
        for name in names:
            self.character_list.insert(tk.END, name)
        self._update_json_preview()

    def _on_character_select(self, _event: tk.Event) -> None:
        selection = self.character_list.curselection()
        if not selection:
            return
        name = self.character_list.get(selection[0])
        raw = self.db.get("characters", {}).get(name, {})
        self.selected_name = name
        self.name_var.set(name)
        self.aliases_var.set(", ".join(raw.get("aliases", [name])))
        self.skill_key_var.set(str(raw.get("skill_key", "e")))
        self.skill_cd_var.set(str(raw.get("skill_cd", 10.0)))
        self.buff_key_var.set(str(raw.get("buff_key", "")))
        self.buff_duration_var.set(str(raw.get("buff_duration", 0.0)))
        self.buff_label_var.set(str(raw.get("buff_label", "")))
        self.db_status_var.set(f"已载入角色: {name}")

    def _new_character(self) -> None:
        self.selected_name = None
        self.name_var.set("")
        self.aliases_var.set("")
        self.skill_key_var.set("e")
        self.skill_cd_var.set("10.0")
        self.buff_key_var.set("")
        self.buff_duration_var.set("0.0")
        self.buff_label_var.set("")
        if hasattr(self, "character_list"):
            self.character_list.selection_clear(0, tk.END)
        self.db_status_var.set("正在新增角色")

    def _save_character(self) -> None:
        try:
            name = self.name_var.get().strip()
            if not name:
                raise ValueError("角色名不能为空")
            aliases = [item.strip() for item in self.aliases_var.get().split(",") if item.strip()]
            if name not in aliases:
                aliases.insert(0, name)
            skill_key = self.skill_key_var.get().strip().lower()
            if not skill_key:
                raise ValueError("技能按键不能为空")
            skill_cd = float(self.skill_cd_var.get())
            buff_duration = float(self.buff_duration_var.get())
            if skill_cd < 0 or buff_duration < 0:
                raise ValueError("时间不能为负数")

            characters = self.db.setdefault("characters", {})
            if self.selected_name and self.selected_name != name:
                characters.pop(self.selected_name, None)
            characters[name] = {
                "aliases": aliases,
                "skill_key": skill_key,
                "skill_cd": skill_cd,
                "buff_key": self.buff_key_var.get().strip().lower(),
                "buff_duration": buff_duration,
                "buff_label": self.buff_label_var.get().strip(),
            }
            self.selected_name = name
            self._save_current_db(f"已保存角色: {name}")
            self._refresh_runtime_database(was_running=self.running)
            self._refresh_character_list()
            self._select_character(name)
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))

    def _delete_character(self) -> None:
        name = self.name_var.get().strip()
        if not name or name not in self.db.get("characters", {}):
            messagebox.showinfo("删除角色", "请先选择一个已存在的角色")
            return
        if not messagebox.askyesno("确认删除", f"确定删除角色 {name} 吗？"):
            return
        self.db["characters"].pop(name, None)
        self.selected_name = None
        self._save_current_db(f"已删除角色: {name}")
        self._refresh_runtime_database(was_running=self.running)
        self._new_character()
        self._refresh_character_list()

    def _open_json(self) -> None:
        path = filedialog.askopenfilename(
            title="打开角色数据库",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        was_running = self.running
        if was_running:
            self.stop_runtime()
        try:
            self.db_path = Path(path)
            self.db = load_raw_database(self.db_path)
            self.characters, self.region, self.settings = load_database(self.db_path)
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc))
            return
        self.region_var.set(f"{self.region.left},{self.region.top},{self.region.width},{self.region.height}")
        self._sync_settings_vars()
        self.db_var.set(f"数据库: {self.db_path}")
        self.db_status_var.set(f"已打开数据库: {self.db_path}")
        self.selected_name = None
        self._new_character()
        self._refresh_character_list()
        if was_running:
            self.start_runtime()

    def _save_as(self) -> None:
        path = filedialog.asksaveasfilename(
            title="另存角色数据库",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            self.db_path = Path(path)
            save_raw_database(self.db_path, self.db)
        except Exception as exc:
            messagebox.showerror("另存失败", str(exc))
            return
        self.db_var.set(f"数据库: {self.db_path}")
        self.db_status_var.set(f"已另存为: {self.db_path}")
        self._update_json_preview()

    def _save_current_db(self, status: str) -> None:
        self.db.setdefault("default_region", f"{self.region.left},{self.region.top},{self.region.width},{self.region.height}")
        self.db.setdefault("settings", settings_to_dict(self.settings))
        save_raw_database(self.db_path, self.db)
        self.db_status_var.set(f"{status} -> {self.db_path}")
        self.db_var.set(f"数据库: {self.db_path}")
        self._update_json_preview()

    def _refresh_runtime_database(self, was_running: bool) -> None:
        if was_running:
            self.stop_runtime()
        self.characters, self.region, self.settings = load_database(self.db_path)
        self.db = load_raw_database(self.db_path)
        self.positions.clear()
        self.slot_characters.clear()
        self.cooldowns.clear()
        self.buffs.clear()
        self.active_slot = None
        self.active_character = None
        if was_running:
            self.start_runtime()

    def _update_json_preview(self) -> None:
        if not hasattr(self, "preview_text"):
            return
        self.preview_text.configure(state=tk.NORMAL)
        self.preview_text.delete("1.0", tk.END)
        self.preview_text.insert(tk.END, json.dumps(self.db, ensure_ascii=False, indent=2))
        self.preview_text.configure(state=tk.DISABLED)

    def _select_character(self, name: str) -> None:
        if not hasattr(self, "character_list"):
            return
        for index in range(self.character_list.size()):
            if self.character_list.get(index) == name:
                self.character_list.selection_set(index)
                self.character_list.see(index)
                break

    def _toggle_keyboard_runtime(self) -> None:
        if not self.running:
            return
        if self.keyboard_enabled_var.get():
            self._start_keyboard_listener()
        else:
            self._stop_keyboard_listener()

    def _toggle_ocr_runtime(self) -> None:
        if not self.running:
            return
        if self.ocr_enabled_var.get():
            self._start_ocr_thread()
        else:
            self._stop_ocr_thread()

    def _toggle_overlay_runtime(self) -> None:
        if not self.overlay_enabled_var.get():
            self._hide_all_overlays()

    def _hide_all_overlays(self) -> None:
        for label in self.cd_labels.values():
            label.hide()
        self.buff_panel.hide()

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    @staticmethod
    def _float_setting(
        variable: tk.StringVar,
        label: str,
        minimum: float | None,
        maximum: float | None,
    ) -> float:
        value = float(variable.get())
        if minimum is not None and value < minimum:
            raise ValueError(f"{label} 不能小于 {minimum}")
        if maximum is not None and value > maximum:
            raise ValueError(f"{label} 不能大于 {maximum}")
        return value

    @staticmethod
    def _int_setting(
        variable: tk.StringVar,
        label: str,
        minimum: int | None,
        maximum: int | None,
    ) -> int:
        value = int(variable.get())
        if minimum is not None and value < minimum:
            raise ValueError(f"{label} 不能小于 {minimum}")
        if maximum is not None and value > maximum:
            raise ValueError(f"{label} 不能大于 {maximum}")
        return value

    def _on_key_event(self, event: Any) -> None:
        if not self.running or not self.keyboard_enabled_var.get():
            return
        if getattr(event, "event_type", "") != "down":
            return
        key = str(getattr(event, "name", "")).lower()
        self.events.put(("key", ("hook", key)))

    def _start_keyboard_listener(self) -> None:
        if self.keyboard_hook is not None:
            self.listener_var.set("键盘监听: 已启动")
            return
        if self.debug:
            print(f"running as admin: {is_user_admin()}")
        try:
            self.keyboard_hook = keyboard.hook(self._on_key_event, suppress=False)
            self.listener_var.set("键盘监听: 已启动")
            if self.debug:
                print("keyboard listener: keyboard hook")
        except Exception as exc:
            self.listener_var.set(f"键盘监听: 启动失败 {exc}")
            if self.debug:
                print(f"keyboard hook failed: {exc}")

    def _stop_keyboard_listener(self) -> None:
        try:
            if self.keyboard_hook is not None:
                keyboard.unhook(self.keyboard_hook)
        except Exception:
            pass
        self.keyboard_hook = None
        self.listener_var.set("键盘监听: 未启动")

    def _start_ocr_thread(self) -> None:
        if self.ocr_thread is not None and self.ocr_thread.is_alive():
            self.ocr_var.set("OCR: 已启动")
            return
        self.ocr_stop_event = threading.Event()
        self.ocr_thread = threading.Thread(
            target=self._ocr_loop,
            args=(self.ocr_stop_event,),
            daemon=True,
        )
        self.ocr_thread.start()
        self.ocr_var.set("OCR: 已启动")

    def _stop_ocr_thread(self) -> None:
        thread = self.ocr_thread
        if self.ocr_stop_event is not None:
            self.ocr_stop_event.set()
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=0.6)
        self.ocr_stop_event = None
        self.ocr_thread = None
        self.ocr_var.set("OCR: 未启动")

    def _restart_ocr_thread(self) -> None:
        self._stop_ocr_thread()
        if self.running and self.ocr_enabled_var.get():
            self._start_ocr_thread()

    def _ocr_loop(self, stop_event: threading.Event) -> None:
        ocr = PartyOcr(self.characters, self.settings.min_match_score)
        with ScreenReader(region=self.region, fps=self.fps) as reader:
            while not stop_event.is_set():
                try:
                    frame = reader.grab()
                    recognized = ocr.read_party(frame, self.region)
                    current_slots = dict(self.slot_characters)
                    if not current_slots or not self._party_changed(recognized, current_slots):
                        self.events.put(("party", recognized))
                    else:
                        confirmed = self._confirm_party_change(
                            ocr=ocr,
                            reader=reader,
                            initial=recognized,
                            current_slots=current_slots,
                            stop_event=stop_event,
                        )
                        if confirmed is not None:
                            self.events.put(("party", confirmed))
                        elif self.debug:
                            print("party change ignored: not confirmed")
                except Exception as exc:
                    if self.debug:
                        print(f"ocr loop error: {type(exc).__name__}: {exc}")
                    time.sleep(0.5)
                self._sleep_interruptibly(self.settings.ocr_interval, stop_event)

    def _confirm_party_change(
        self,
        ocr: PartyOcr,
        reader: ScreenReader,
        initial: list[RecognizedCharacter],
        current_slots: dict[int, str],
        stop_event: threading.Event,
    ) -> list[RecognizedCharacter] | None:
        samples = [initial]
        for _index in range(max(0, self.settings.change_confirm_frames - 1)):
            self._sleep_interruptibly(self.settings.change_confirm_interval, stop_event)
            if stop_event.is_set():
                return None
            frame = reader.grab()
            samples.append(ocr.read_party(frame, self.region))

        required_votes = max(2, (len(samples) // 2) + 1)
        slot_votes: dict[int, dict[str, int]] = {}
        best_seen: dict[tuple[int, str], RecognizedCharacter] = {}
        for sample in samples:
            seen_slots: set[int] = set()
            for item in sample:
                if item.slot in seen_slots:
                    continue
                seen_slots.add(item.slot)
                slot_votes.setdefault(item.slot, {})
                slot_votes[item.slot][item.name] = slot_votes[item.slot].get(item.name, 0) + 1
                key = (item.slot, item.name)
                previous = best_seen.get(key)
                if previous is None or item.confidence > previous.confidence:
                    best_seen[key] = item

        confirmed: list[RecognizedCharacter] = []
        changed = False
        for slot, votes in slot_votes.items():
            name, count = max(votes.items(), key=lambda entry: entry[1])
            if count < required_votes:
                continue
            item = best_seen[(slot, name)]
            confirmed.append(item)
            if current_slots.get(slot) != name:
                changed = True

        if not changed:
            return None
        if self.debug:
            order = " | ".join(
                f"{item.slot}:{item.name}/{item.confidence:.2f}" for item in confirmed
            )
            print(f"party change confirmed: {order}")
        return sorted(confirmed, key=lambda item: item.slot)

    @staticmethod
    def _party_changed(
        recognized: list[RecognizedCharacter],
        current_slots: dict[int, str],
    ) -> bool:
        for item in recognized:
            current = current_slots.get(item.slot)
            if current is not None and current != item.name:
                return True
            if current is None:
                return True
        return False

    def _sleep_interruptibly(
        self,
        seconds: float,
        stop_event: threading.Event | None = None,
    ) -> None:
        event = stop_event or self.stop_event
        deadline = time.monotonic() + max(0.0, seconds)
        while not event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.2, remaining))

    def _tick(self) -> None:
        if self.running:
            self._drain_events()
            if self.overlay_enabled_var.get():
                self._update_overlay()
            else:
                self._hide_all_overlays()
        else:
            self._hide_all_overlays()
        self._update_control_status()
        self.after(50, self._tick)

    def _update_control_status(self) -> None:
        if self.running:
            self.status_var.set("状态: 运行中")
        self.listener_var.set(
            "键盘监听: 已启动" if self.keyboard_hook is not None else "键盘监听: 未启动"
        )
        self.ocr_var.set(
            "OCR: 已启动"
            if self.ocr_thread is not None and self.ocr_thread.is_alive()
            else "OCR: 未启动"
        )
        self.active_var.set(f"当前角色: {self._debug_active()}")
        if self.slot_characters:
            party = " | ".join(
                f"{slot}:{self.slot_characters.get(slot, '-')}" for slot in range(1, PARTY_SLOT_COUNT + 1)
            )
            self.party_var.set(f"队伍: {party}")
        else:
            self.party_var.set("队伍: -")
        now = time.monotonic()
        timers: list[str] = []
        for state in sorted(self.cooldowns.values(), key=lambda item: item.end_at):
            remaining = max(0.0, state.end_at - now)
            timers.append(f"CD {state.name} {remaining:04.1f}s")
        for state in sorted(self.buffs.values(), key=lambda item: item.end_at):
            remaining = max(0.0, state.end_at - now)
            timers.append(f"Buff {state.label} {remaining:04.1f}s")
        self.timer_var.set("计时: " + (" | ".join(timers[:6]) if timers else "-"))

    def _drain_events(self) -> None:
        while True:
            try:
                event, payload = self.events.get_nowait()
            except queue.Empty:
                return
            if event == "party":
                self._update_party(payload)
            elif event == "key":
                source, key = payload
                self._handle_key(source, key)

    def _update_party(self, recognized: list[RecognizedCharacter]) -> None:
        now = time.monotonic()
        if not recognized:
            if now - self.last_party_at > self.settings.party_hold_seconds:
                self.positions = {}
                self.slot_characters = {}
                self.active_character = None
                if self.debug:
                    print(f"party: empty; active={self._debug_active()}")
            return
        self.last_party_at = now
        for item in recognized:
            previous_name = self.slot_characters.get(item.slot)
            if previous_name is not None and previous_name != item.name:
                self.positions.pop(previous_name, None)
            self.slot_characters[item.slot] = item.name
            self.positions[item.name] = item
        if self.active_slot is None:
            first = min(recognized, key=lambda item: item.slot)
            self.active_slot = first.slot
        self.active_character = self.slot_characters.get(self.active_slot)
        if self.debug and recognized:
            order = " | ".join(
                f"{item.slot}:{item.name}@({item.x},{item.y})/{item.confidence:.2f}"
                for item in recognized
            )
            print(f"party: {order}; active={self._debug_active()}")

    def _handle_key(self, source: str, key: str) -> None:
        key = self._normalize_key_name(key)
        now = time.monotonic()
        last_at = self.last_key_at.get(key, 0.0)
        if now - last_at < self.settings.key_debounce_seconds:
            return
        self.last_key_at[key] = now
        if key in PARTY_SWITCH_KEYS:
            self._switch_active_slot(int(key), source)
            return
        trigger_keys = {
            config.skill_key
            for config in self.characters.values()
            if config.skill_key
        } | {
            config.buff_key
            for config in self.characters.values()
            if config.buff_key
        }
        if self.debug and key in trigger_keys:
            print(
                f"key down: source={source} key={key} "
                f"active={self._debug_active()}"
            )
        if self.active_character is None:
            return
        config = self.characters.get(self.active_character)
        if config is None:
            return
        if key == config.skill_key:
            if self.debug:
                print(f"skill key: {key}; start {config.name} cd={config.skill_cd}s")
            self.cooldowns[config.name] = TimerState(
                name=config.name,
                label=config.skill_key.upper(),
                end_at=now + config.skill_cd,
                duration=config.skill_cd,
            )
        if config.buff_duration > 0 and key == config.buff_key:
            label = config.buff_label or config.name
            if self.debug:
                print(f"buff key: {key}; start {label} duration={config.buff_duration}s")
            self.buffs[config.name] = TimerState(
                name=config.name,
                label=label,
                end_at=now + config.buff_duration,
                duration=config.buff_duration,
            )

    def _switch_active_slot(self, slot: int, source: str) -> None:
        self.active_slot = slot
        self.active_character = self.slot_characters.get(slot)
        if self.debug:
            print(
                f"switch slot: source={source} slot={slot} "
                f"active={self._debug_active()}"
            )

    @staticmethod
    def _normalize_key_name(key: str) -> str:
        key = key.strip().lower()
        if key.startswith("num "):
            suffix = key.removeprefix("num ").strip()
            if suffix in PARTY_SWITCH_KEYS:
                return suffix
        if key.startswith("numpad"):
            suffix = key.removeprefix("numpad").strip()
            if suffix in PARTY_SWITCH_KEYS:
                return suffix
        return key

    def _debug_active(self) -> str:
        slot = self.active_slot if self.active_slot is not None else "-"
        character = self.active_character or "-"
        return f"{slot}:{character}"

    def _update_overlay(self) -> None:
        now = time.monotonic()
        active_names = set(self.positions)
        for name in list(self.cooldowns):
            if self.cooldowns[name].end_at <= now:
                del self.cooldowns[name]
        for name in list(self.buffs):
            if self.buffs[name].end_at <= now:
                del self.buffs[name]

        for name, state in self.cooldowns.items():
            position = self.positions.get(name)
            label = self.cd_labels.get(name)
            if label is None:
                label = OverlayLabel(
                    self,
                    fg="#00fff0",
                    bg="#111111",
                    font=("Segoe UI", self.settings.cd_label_font_size, "bold"),
                    alpha=self.settings.cd_label_alpha,
                )
                self.cd_labels[name] = label
            if position is None:
                label.hide()
                continue
            remaining = max(0.0, state.end_at - now)
            progress = remaining / state.duration if state.duration > 0 else 0.0
            x = max(0, self.region.left - self.settings.cd_label_left_offset)
            y = position.y + max(0, position.height // 2 - 18)
            label.show(f"{remaining:04.1f}", x, y, progress)

        for name, label in list(self.cd_labels.items()):
            if name not in self.cooldowns or name not in active_names:
                label.hide()

        self._update_buff_overlay(now)

    def _update_buff_overlay(self, now: float) -> None:
        if not self.buffs:
            self.buff_panel.hide()
            return
        rows: list[tuple[str, float, float]] = []
        for state in sorted(self.buffs.values(), key=lambda item: item.end_at):
            remaining = max(0.0, state.end_at - now)
            progress = remaining / state.duration if state.duration > 0 else 0.0
            rows.append((state.label, remaining, progress))
        if not rows:
            self.buff_panel.hide()
            return
        x = max(0, self.region.left - self.settings.buff_panel_left_offset)
        y = max(0, self.region.top - self.settings.buff_panel_top_offset)
        self.buff_panel.show(rows, x, y)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=default_db_path())
    parser.add_argument("--region", type=CaptureRegion.parse)
    parser.add_argument("--fps", type=float, default=2.0)
    parser.add_argument("--min-match-score", type=float)
    parser.add_argument(
        "--ocr-interval",
        type=float,
        help="seconds between normal OCR checks",
    )
    parser.add_argument(
        "--change-confirm-frames",
        type=int,
        help="extra OCR samples used before accepting a party change",
    )
    parser.add_argument(
        "--change-confirm-interval",
        type=float,
        help="seconds between confirmation OCR samples",
    )
    parser.add_argument("--party-hold-seconds", type=float)
    parser.add_argument("--key-debounce-seconds", type=float)
    parser.add_argument("--cd-label-left-offset", type=int)
    parser.add_argument("--cd-label-font-size", type=int)
    parser.add_argument("--cd-label-alpha", type=float)
    parser.add_argument("--buff-panel-left-offset", type=int)
    parser.add_argument("--buff-panel-top-offset", type=int)
    parser.add_argument("--buff-panel-font-size", type=int)
    parser.add_argument("--buff-panel-alpha", type=float)
    parser.add_argument("--auto-start", action="store_true", help="start OCR and keyboard listener immediately")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--log", type=Path)
    return parser.parse_args()


def setup_file_logging(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    log_file = path.open("a", encoding="utf-8", buffering=1)
    sys.stdout = log_file
    sys.stderr = log_file
    print(f"\n--- CharacterTimerOverlay started {time.strftime('%Y-%m-%d %H:%M:%S')} ---")


def main() -> int:
    args = parse_args()
    if args.log is not None:
        setup_file_logging(args.log)
    characters, db_region, settings = load_database(args.db)
    region = args.region or db_region or DEFAULT_REGION
    overrides = {
        "min_match_score": args.min_match_score,
        "ocr_interval": args.ocr_interval,
        "change_confirm_frames": args.change_confirm_frames,
        "change_confirm_interval": args.change_confirm_interval,
        "party_hold_seconds": args.party_hold_seconds,
        "key_debounce_seconds": args.key_debounce_seconds,
        "cd_label_left_offset": args.cd_label_left_offset,
        "cd_label_font_size": args.cd_label_font_size,
        "cd_label_alpha": args.cd_label_alpha,
        "buff_panel_left_offset": args.buff_panel_left_offset,
        "buff_panel_top_offset": args.buff_panel_top_offset,
        "buff_panel_font_size": args.buff_panel_font_size,
        "buff_panel_alpha": args.buff_panel_alpha,
    }
    settings = replace(
        settings,
        **{key: value for key, value in overrides.items() if value is not None},
    )
    settings = load_settings(settings.__dict__)
    app = TimerOverlayApp(
        db_path=args.db,
        characters=characters,
        region=region,
        fps=args.fps,
        settings=settings,
        debug=args.debug,
        auto_start=args.auto_start,
    )
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
