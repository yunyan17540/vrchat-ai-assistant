from __future__ import annotations

import json
import logging
import os
import queue
import socket
import struct
import tempfile
import threading
import time
import ctypes
from ctypes import wintypes
from pathlib import Path

import openvr
from PIL import Image, ImageDraw, ImageFont, ImageGrab

from vrchat_ai_translator import ask_model, clean_reply, load_settings


ROOT_DIR = Path(__file__).resolve().parent
CONTROL_PATH = ROOT_DIR / "steamvr_controls.json"
ACTION_MANIFEST_PATH = ROOT_DIR / "steamvr_actions_v2.json"
OVERLAY_PREFIX = "vrchat-ai-translator"
LOG_PATH = ROOT_DIR / "steamvr_overlay.log"
DASHBOARD_KEY = f"{OVERLAY_PREFIX}.settings"
DASHBOARD_SIZE = (1600, 1000)
STOP_PATH = ROOT_DIR / ".steamvr_overlay.stop"
PID_PATH = ROOT_DIR / ".steamvr_overlay.pid"
DASHBOARD_CHOICES = [
    "left_grip", "right_grip", "left_trigger", "right_trigger", "trigger",
    "left_a", "right_a", "left_b", "right_b", "left_touchpad", "right_touchpad",
    "left_menu", "right_menu",
]
CONFIRM_CHOICES = [
    "left_trigger", "left_grip", "left_a", "left_b", "left_touchpad", "left_menu",
]
ACTION_INPUTS = [
    "left_grip", "right_grip", "left_trigger", "right_trigger", "left_a", "right_a",
    "left_b", "right_b", "left_touchpad", "right_touchpad", "left_menu", "right_menu",
]
ANALOG_ACTIONS = ["right_joystick"]
logging.basicConfig(filename=LOG_PATH, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BINDING_DEFAULTS = {
    "translate": "right_trigger",
    "confirm_reply": "left_trigger",
}
OUTPUT_DEFAULTS = {
    "text_output": "tapi_osc",
    "clipboard_capture": "off",
    "hud_visibility": "auto",
    "recognition_enabled": "on",
}

LONG_PRESS_SECONDS = 0.75
HOVER_STABLE_SECONDS = 0.12
JOYSTICK_SELECTION_HOLD_SECONDS = 2.5
TRIGGER_AXIS_THRESHOLD = 0.75
JOYSTICK_DEADZONE = 0.65

BUTTON_CODES = {
    "menu": openvr.k_EButton_ApplicationMenu,
    "grip": openvr.k_EButton_Grip,
    "a": openvr.k_EButton_A,
    "b": openvr.k_EButton_IndexController_B,
    "touchpad": openvr.k_EButton_SteamVR_Touchpad,
    "trigger": openvr.k_EButton_SteamVR_Trigger,
}

# HUD cards placed in the lower center of the user's view.
OVERLAY_SPECS = {
    "context": ((2200, 430), 1.00, (0.00, 0.15, -1.25)),
    "reply0": ((2200, 260), 1.00, (0.00, -0.24, -1.20)),
    "reply1": ((2200, 260), 1.00, (0.00, -0.39, -1.20)),
    "reply2": ((2200, 260), 1.00, (0.00, -0.54, -1.20)),
    "close": ((2200, 220), 1.00, (0.00, -0.69, -1.20)),
}

SELECTION_COUNT = 4

FONT_CANDIDATES = [
    Path(r"C:\Windows\Fonts\NotoSansSC-VF.ttf"),
    Path(r"C:\Windows\Fonts\simhei.ttf"),
]

INK = (12, 14, 15, 255)
MUTED = (51, 57, 58, 220)
GLASS = (255, 255, 255, 112)
GLASS_HOVER = (255, 255, 255, 205)
GLASS_PRESSED = (255, 255, 255, 235)
BORDER = (10, 12, 13, 245)
ACCENT = (255, 255, 255, 205)


def pick_font(size: int, weight: bytes = b"Medium"):
    for path in FONT_CANDIDATES:
        if path.exists():
            font = ImageFont.truetype(str(path), size)
            # Noto Sans SC is a variable font; Medium keeps small VR text from
            # disappearing against a bright world while retaining the requested
            # Source Han Sans look.
            try:
                font.set_variation_by_name(weight)
            except Exception:
                pass
            return font
    return ImageFont.load_default()


def load_bindings() -> dict[str, str]:
    values = dict(BINDING_DEFAULTS)
    values.update(OUTPUT_DEFAULTS)
    try:
        if CONTROL_PATH.exists():
            loaded = json.loads(CONTROL_PATH.read_text(encoding="utf-8"))
            for key in values:
                if isinstance(loaded.get(key), str):
                    values[key] = loaded[key]
    except Exception:
        pass
    # Migrate earlier prototype defaults so an old controls file cannot leave
    # recognition on a grip or selection on both triggers.
    if values.get("translate") == "right_grip":
        values["translate"] = "right_trigger"
    if values.get("confirm_reply") not in CONFIRM_CHOICES:
        values["confirm_reply"] = "left_trigger"
    values.pop("custom_input", None)
    values.pop("toggle_visibility", None)
    if values.get("recognition_enabled") not in {"on", "off"}:
        values["recognition_enabled"] = "on"
    if not CONTROL_PATH.exists():
        CONTROL_PATH.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
    return values


def binding_label(binding: str) -> str:
    return {
        "left_grip": "左手握把",
        "right_grip": "右手握把",
        "left_menu": "左手菜单键",
        "right_menu": "右手菜单键",
        "left_a": "左手 A",
        "right_a": "右手 A",
        "left_b": "左手 B",
        "right_b": "右手 B",
        "left_touchpad": "左手触控板",
        "right_touchpad": "右手触控板",
        "left_trigger": "左手扳机",
        "right_trigger": "右手扳机",
        "trigger": "任一手扳机",
    }.get(binding, binding)


def option_label(key: str, value: str) -> str:
    choices = {
        "text_output": {
            "keyboard": "键盘粘贴并回车",
            "tapi_osc": "Tapi / VRChat OSC",
        },
        "clipboard_capture": {
            "off": "关闭",
            "on": "开启",
        },
        "hud_visibility": {
            "auto": "识别后显示",
            "always_hidden": "始终隐藏",
        },
        "recognition_enabled": {
            "on": "开启（右扳机可识别）",
            "off": "已关闭（长按后锁定）",
        },
    }
    return choices.get(key, {}).get(value, value)


def new_image(size: tuple[int, int]):
    image = Image.new("RGBA", size, (255, 255, 255, 3))
    return image, ImageDraw.Draw(image)


def draw_frame(draw: ImageDraw.ImageDraw, size: tuple[int, int], hover: bool = False):
    width, height = size
    scale = width / 760.0
    fill = GLASS_HOVER if hover else GLASS
    pad = max(6, int(6 * scale))
    draw.rounded_rectangle((pad, pad, width - pad, height - pad), radius=int(14 * scale), fill=fill, outline=BORDER, width=max(2, int(2 * scale)))
    inner = max(14, int(14 * scale))
    draw.rounded_rectangle((inner, inner, width - inner, height - inner), radius=int(9 * scale), outline=ACCENT, width=max(1, int(scale)))
    line = int(115 * scale)
    edge = int(27 * scale)
    top = int(25 * scale)
    draw.line((edge, top, line, top), fill=ACCENT, width=max(2, int(2 * scale)))
    draw.line((width - line, height - top, width - edge, height - top), fill=ACCENT, width=max(2, int(2 * scale)))


def draw_one_line(draw, text: str, xy, font, fill, max_width: int):
    text = str(text or "").strip() or "等待识别…"
    while text and draw.textbbox((0, 0), text, font=font)[2] > max_width:
        text = text[:-2] + "…"
    draw.text(xy, text, font=font, fill=fill)


def draw_wrapped(draw, text: str, xy, font, fill, max_width: int, max_lines: int = 2, line_gap: int = 8):
    """Wrap mixed CJK/Latin text by measured pixels so no glyph is cut off."""
    text = str(text or "").strip() or "等待回复…"
    lines: list[str] = []
    current = ""
    for char in text:
        candidate = current + char
        if current and draw.textbbox((0, 0), candidate, font=font)[2] > max_width:
            lines.append(current)
            current = char
        else:
            current = candidate
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and draw.textbbox((0, 0), last + "…", font=font)[2] > max_width:
            last = last[:-1]
        lines[-1] = (last or "…") + "…"
    x, y = xy
    for index, line in enumerate(lines):
        draw.text((x, y + index * (font.size + line_gap)), line, font=font, fill=fill)
    return len(lines)


def context_image(state: dict) -> Image.Image:
    size = OVERLAY_SPECS["context"][0]
    image, draw = new_image(size)
    draw_frame(draw, size)
    draw.text((72, 42), "AI 识别", font=pick_font(54, b"Bold"), fill=INK)
    draw.text((76, 116), f"{binding_label(state['bindings']['translate'])}：识别当前画面", font=pick_font(32), fill=MUTED)
    draw.text((76, 206), "识别内容", font=pick_font(32, b"Bold"), fill=MUTED)
    draw_wrapped(draw, state.get("detected", ""), (330, 196), pick_font(40, b"Bold"), INK, size[0] - 430, max_lines=2)
    draw.text((76, 316), "中文含义", font=pick_font(32, b"Bold"), fill=MUTED)
    draw_wrapped(draw, state.get("meaning", ""), (330, 306), pick_font(38, b"Bold"), INK, size[0] - 430, max_lines=2)
    return image


def clipboard_image(text: str) -> Image.Image:
    """Render clipboard text into a neutral image for the vision model."""
    size = (2200, 900)
    image, draw = new_image(size)
    draw_frame(draw, size)
    draw.text((78, 48), "剪贴板识别", font=pick_font(54, b"Bold"), fill=INK)
    draw_wrapped(draw, text, (78, 150), pick_font(52, b"Bold"), INK, size[0] - 160, max_lines=10, line_gap=16)
    return image


def reply_image(index: int, state: dict) -> Image.Image:
    size = OVERLAY_SPECS[f"reply{index}"][0]
    image, draw = new_image(size)
    is_hover = state.get("hover_reply") == index
    is_pressed = state.get("pressed_reply") == index
    draw_frame(draw, size, hover=is_hover or is_pressed)
    if is_pressed:
        pad = max(6, int(6 * (size[0] / 760.0)))
        draw.rounded_rectangle(
            (pad, pad, size[0] - pad, size[1] - pad),
            radius=int(14 * (size[0] / 760.0)),
            fill=GLASS_PRESSED,
            outline=(0, 0, 0, 255),
            width=max(3, int(3 * (size[0] / 760.0))),
        )
    item = state.get("replies", [])[index] if index < len(state.get("replies", [])) else {}
    text = str(item.get("text", ""))
    translation = str(item.get("translation_zh", ""))
    draw.text((72, 66), str(index + 1), font=pick_font(72, b"Bold"), fill=INK)
    main_font = pick_font(64, b"Bold")
    translation_font = pick_font(40, b"Medium")
    max_width = size[0] - 700
    main_lines = draw_wrapped(draw, text, (230, 26), main_font, INK, max_width, max_lines=2, line_gap=8)
    if translation:
        translation_y = 26 + main_lines * (main_font.size + 8) + 10
        draw_wrapped(draw, f"（{translation}）", (230, translation_y), translation_font, MUTED, max_width, max_lines=1)
    draw.text((size[0] - 430, 102), binding_label(state["bindings"]["confirm_reply"]), font=pick_font(34, b"Medium"), fill=MUTED)
    return image


def close_image(state: dict) -> Image.Image:
    size = OVERLAY_SPECS["close"][0]
    image, draw = new_image(size)
    draw_frame(draw, size, hover=state.get("hover_reply") == "close")
    draw.text((72, 45), "×", font=pick_font(72, b"Bold"), fill=INK)
    draw.text((230, 32), "关闭该次会话", font=pick_font(52, b"Bold"), fill=INK)
    draw.text((230, 118), "清空识别内容并收起 HUD", font=pick_font(32), fill=MUTED)
    return image


def dashboard_candidates(key: str) -> list[str]:
    if key == "translate":
        return ["right_trigger", "right_grip", "right_a", "right_b", "right_touchpad", "right_menu"]
    if key == "confirm_reply":
        return list(CONFIRM_CHOICES)
    if key == "recognition_enabled":
        return ["on", "off"]
    if key == "hud_visibility":
        return ["auto", "always_hidden"]
    if key == "text_output":
        return ["keyboard", "tapi_osc"]
    if key == "clipboard_capture":
        return ["off", "on"]
    return []


def dashboard_choice_label(key: str, value: str) -> str:
    return binding_label(value) if key in {"translate", "confirm_reply"} else option_label(key, value)


def dashboard_image(bindings: dict[str, str], saved: bool = False, picker_key: str | None = None) -> Image.Image:
    size = DASHBOARD_SIZE
    image = Image.new("RGBA", size, (248, 250, 249, 255))
    draw = ImageDraw.Draw(image)
    ink = (25, 28, 29, 255)
    muted = (88, 96, 97, 255)
    panel = (255, 255, 255, 228)
    accent = (190, 132, 154, 255)
    draw.rectangle((0, 0, size[0] - 1, size[1] - 1), fill=(248, 250, 249, 255), outline=(18, 20, 21, 255), width=4)
    draw.line((52, 132, size[0] - 52, 132), fill=accent, width=3)
    draw.text((64, 44), "VRChat AI 回复助手", font=pick_font(46), fill=ink)
    draw.text((66, 94), "SteamVR 设置 · 点击一行后，在右侧候选项中选择", font=pick_font(22), fill=muted)
    draw.text((66, 146), "控制与发送", font=pick_font(32), fill=ink)
    rows = [
        ("一键识别当前画面", "translate", True),
        ("选择回复", "confirm_reply", True),
        ("识别功能总开关", "recognition_enabled", False),
        ("HUD显示方式", "hud_visibility", False),
        ("回复发送方式", "text_output", False),
        ("剪贴板识别", "clipboard_capture", False),
    ]
    y = 190
    for label, key, is_binding in rows:
        draw.rounded_rectangle((66, y, 1120, y + 78), radius=14, fill=panel, outline=(24, 27, 28, 255), width=2)
        draw.text((98, y + 23), label, font=pick_font(24, b"Bold"), fill=ink)
        draw.rounded_rectangle((715, y + 12, 1085, y + 66), radius=10, fill=(247, 246, 247, 255), outline=accent, width=2)
        value = dashboard_choice_label(key, bindings.get(key, ""))
        draw.text((742, y + 26), value, font=pick_font(19, b"Bold"), fill=ink)
        y += 90
    draw.rounded_rectangle((1180, 164, 1534, 846), radius=16, fill=panel, outline=(24, 27, 28, 255), width=2)
    if picker_key:
        rows_by_key = {
            "translate": "一键识别当前画面",
            "confirm_reply": "选择回复",
            "recognition_enabled": "识别功能总开关",
            "hud_visibility": "HUD显示方式",
            "text_output": "回复发送方式",
            "clipboard_capture": "剪贴板识别",
        }
        draw.text((1210, 198), "选择候选项", font=pick_font(30), fill=ink)
        draw.text((1210, 244), rows_by_key.get(picker_key, picker_key), font=pick_font(22, b"Bold"), fill=muted)
        choices = dashboard_candidates(picker_key)
        for index, choice in enumerate(choices[:6]):
            top = 286 + index * 82
            active = bindings.get(picker_key) == choice
            draw.rounded_rectangle(
                (1208, top, 1508, top + 62),
                radius=10,
                fill=accent if active else (247, 246, 247, 255),
                outline=(24, 27, 28, 255),
                width=2,
            )
            draw.text((1230, top + 18), dashboard_choice_label(picker_key, choice), font=pick_font(19, b"Bold"), fill=ink)
        draw.text((1210, 790), "点击候选项立即选中", font=pick_font(19), fill=muted)
    else:
        draw.text((1210, 198), "快速教程", font=pick_font(30), fill=ink)
        tutorial_lines = [
            "1 右扳机短按：识别",
            "2 右扳机长按：关闭并锁定识别",
            "3 右摇杆上下：选择 1-4",
            "4 左扳机：确认当前选项",
            "4 第 4 项：关闭本次会话",
            "识别前 HUD 保持隐藏，",
            "关闭后请在本页重新开启。",
        ]
        for index, line in enumerate(tutorial_lines):
            draw.text((1210, 250 + index * 64), line, font=pick_font(21), fill=muted if index >= 6 else ink)
    draw.text((66, 910), "右扳机短按识别，长按关闭并锁定；右摇杆选择，左手按键确认。", font=pick_font(19), fill=muted)
    draw.rounded_rectangle((1280, 866, 1518, 958), radius=14, fill=accent if saved else (255, 255, 255, 240), outline=(24, 27, 28, 255), width=2)
    draw.text((1345, 895), "已保存" if saved else "保存设置", font=pick_font(23), fill=ink)
    return image


def dashboard_thumbnail() -> Image.Image:
    image = Image.new("RGBA", (256, 256), (24, 27, 31, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 10, 246, 246), outline=(245, 247, 248, 255), width=5)
    draw.line((34, 86, 222, 86), fill=(210, 143, 168, 255), width=4)
    draw.text((54, 42), "AI", font=pick_font(38), fill=(245, 247, 248, 255))
    draw.text((34, 115), "VRChat", font=pick_font(28), fill=(245, 247, 248, 255))
    draw.text((47, 158), "设置", font=pick_font(28), fill=(210, 143, 168, 255))
    return image


class SteamVROverlay:
    def __init__(self):
        self.bindings = load_bindings()
        self.recognition_enabled = self.bindings.get("recognition_enabled", "on") == "on"
        logging.info("Loaded controls: %s", self.bindings)
        self.state = {
            "detected": "",
            "meaning": "",
            "replies": [],
            "hover_reply": None,
            "pressed_reply": None,
            "busy": False,
            "bindings": self.bindings,
        }
        self.state_lock = threading.Lock()
        self.temp_dir = Path(tempfile.mkdtemp(prefix="vrchat-ai-hud-"))
        self.running = True
        self.visible = False
        self.has_result = False
        self.overlay_api = None
        self.vr_system = None
        self.vr_input = None
        self.action_set_handle = 0
        self.action_handles: dict[str, int] = {}
        self.analog_handles: dict[str, int] = {}
        self.input_ready = False
        self.hotkeys_registered = False
        self.dashboard_handle = None
        self.dashboard_thumbnail_handle = None
        self.dashboard_path = self.temp_dir / "dashboard.png"
        self.dashboard_thumbnail_path = self.temp_dir / "dashboard_thumbnail.png"
        self.dashboard_saved = False
        self.dashboard_picker_key: str | None = None
        self.last_dashboard_click_at = 0.0
        self.last_dashboard_click_signature: tuple[int, int] | None = None
        self.handles: dict[str, int] = {}
        self.paths: dict[str, Path] = {}
        self.render_serial = 0
        self.previous_buttons: dict[int, int] = {}
        self.hovered_reply: int | None = None
        self.selected_reply: int = 0
        self.next_joystick_move_at = 0.0
        self.hover_candidate = None
        self.hover_candidate_since = 0.0
        self.joystick_selected_until = 0.0
        self.last_controller_device: int | None = None
        self.pending_hide_at: float | None = None
        self.input_feedback_hide_at: float | None = None
        self.pending_paste_text: str | None = None
        self.keyboard_open = False
        self.result_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.keyboard_state = {0x77: False}  # F8 identify fallback only
        self.button_press_started: dict[str, float] = {}
        self.long_press_fired: set[str] = set()
        self.digital_inputs_armed = False
        self.legacy_trigger_states: dict[str, bool] = {"left": False, "right": False}
        self.legacy_trigger_started: dict[str, float] = {}
        self.legacy_long_press_fired: set[str] = set()
        self.action_trigger_active = False
        self.action_joystick_active = False
        self.action_digital_event_seen = False
        self.action_joystick_event_seen = False
        self.fallback_controller_roles: dict[int, str] = {}
        self.controller_state_signatures: dict[int, tuple] = {}
        self.last_input_diag_at = 0.0

    def _new_overlay(self, key: str, name: str) -> None:
        _, width_m, position = OVERLAY_SPECS[key]
        handle = self.overlay_api.createOverlay(f"{OVERLAY_PREFIX}.{key}", name)
        self.overlay_api.setOverlayWidthInMeters(handle, width_m)
        # HUD cards are display-only. Selection is deliberately controller-only;
        # accepting laser/mouse input made the target jump unpredictably.
        interactive = False
        self.overlay_api.setOverlayInputMethod(
            handle,
            openvr.VROverlayInputMethod_Mouse if interactive else openvr.VROverlayInputMethod_None,
        )
        self.overlay_api.setOverlayFlag(handle, openvr.VROverlayFlags_EnableClickStabilization, interactive)
        self.overlay_api.setOverlayFlag(handle, openvr.VROverlayFlags_MakeOverlaysInteractiveIfVisible, interactive)
        self.overlay_api.setOverlayFlag(handle, openvr.VROverlayFlags_WantsModalBehavior, False)
        transform = openvr.HmdMatrix34_t()
        transform.m[0][0] = 1.0
        transform.m[1][1] = 1.0
        transform.m[2][2] = 1.0
        transform.m[0][3], transform.m[1][3], transform.m[2][3] = position
        self.overlay_api.setOverlayTransformTrackedDeviceRelative(handle, openvr.k_unTrackedDeviceIndex_Hmd, transform)
        self.handles[key] = handle
        self.paths[key] = self.temp_dir / f"{key}.png"

    def _destroy_existing(self, key: str) -> None:
        """Remove only stale overlays owned by this assistant."""
        try:
            old = self.overlay_api.findOverlay(key)
            self.overlay_api.destroyOverlay(old)
        except Exception:
            pass

    def setup(self):
        openvr.init(openvr.VRApplication_Overlay)
        try:
            self.vr_input = openvr.VRInput()
            self.vr_input.setActionManifestPath(str(ACTION_MANIFEST_PATH))
            self.action_set_handle = self.vr_input.getActionSetHandle("/actions/main")
            self.action_handles = {
                name: self.vr_input.getActionHandle(f"/actions/main/in/{name}")
                for name in ACTION_INPUTS
            }
            self.analog_handles = {
                name: self.vr_input.getActionHandle(f"/actions/main/in/{name}")
                for name in ANALOG_ACTIONS
            }
            self.input_ready = True
            logging.info("SteamVR Input action manifest ready")
        except Exception:
            self.input_ready = False
            logging.exception("SteamVR Input action manifest unavailable; using legacy events")
        # SteamVR Input is the primary path. Keep the desktop F8 fallback
        # completely out of the way when the action manifest loaded, so a
        # stale keyboard message can never start recognition in normal VR use.
        if not self.input_ready:
            self.register_fallback_hotkeys()
        else:
            logging.info("SteamVR Input ready; desktop F8 fallback disabled")
        self.overlay_api = openvr.VROverlay()
        self.vr_system = openvr.VRSystem()
        for key in OVERLAY_SPECS:
            self._destroy_existing(f"{OVERLAY_PREFIX}.{key}")
        for key in OVERLAY_SPECS:
            self._new_overlay(key, key)
        self._destroy_existing(DASHBOARD_KEY)
        self.dashboard_handle, self.dashboard_thumbnail_handle = self.overlay_api.createDashboardOverlay(
            DASHBOARD_KEY, "VRChat AI 回复助手"
        )
        self.overlay_api.setOverlayWidthInMeters(self.dashboard_handle, 1.85)
        self.overlay_api.setOverlayInputMethod(self.dashboard_handle, openvr.VROverlayInputMethod_Mouse)
        self.overlay_api.setOverlayFlag(
            self.dashboard_handle,
            openvr.VROverlayFlags_MakeOverlaysInteractiveIfVisible,
            True,
        )
        self.overlay_api.setOverlayFlag(
            self.dashboard_handle,
            openvr.VROverlayFlags_WantsModalBehavior,
            True,
        )
        self.overlay_api.setOverlayInputMethod(self.dashboard_thumbnail_handle, openvr.VROverlayInputMethod_Mouse)
        mouse_scale = openvr.HmdVector2_t()
        mouse_scale.v[0], mouse_scale.v[1] = DASHBOARD_SIZE
        self.overlay_api.setOverlayMouseScale(self.dashboard_handle, mouse_scale)
        self.render_all()
        self.hide_all()

    def show_all(self):
        if not self.has_result or self.bindings.get("hud_visibility", "auto") == "always_hidden":
            return
        for handle in self.handles.values():
            self.overlay_api.showOverlay(handle)
        self.visible = True
        self.selected_reply = 0
        self.hovered_reply = None
        self.hover_candidate = None
        self.hover_candidate_since = time.monotonic()

    def show_context_only(self):
        """Show only the recognition status card while the model is working."""
        if self.bindings.get("hud_visibility", "auto") == "always_hidden":
            return
        self.overlay_api.showOverlay(self.handles["context"])
        for key in ("reply0", "reply1", "reply2", "close"):
            self.overlay_api.hideOverlay(self.handles[key])
        self.visible = True
        self.hovered_reply = None
        self.hover_candidate = None
        self.hover_candidate_since = time.monotonic()

    def show_input_feedback(self, title: str, detail: str, duration: float = 0.55):
        """Show a short, non-interactive trigger status card in the HUD."""
        if not self.recognition_enabled:
            logging.info("Input feedback suppressed while recognition is disabled: %s", title)
            return
        with self.state_lock:
            self.state["detected"] = title
            self.state["meaning"] = detail
            self.state["replies"] = []
            self.state["busy"] = False
        self.has_result = False
        self.render_all()
        self.show_context_only()
        self.input_feedback_hide_at = time.monotonic() + duration
        if self.last_controller_device is not None and self.vr_system is not None:
            try:
                # 120us is below the perceptible range on many Quest/VD
                # runtimes; use a short but clearly noticeable pulse.
                self.vr_system.triggerHapticPulse(self.last_controller_device, 0, 1500)
            except Exception:
                pass
        logging.info("Input feedback shown title=%s detail=%s", title, detail)

    def hide_all(self):
        for handle in self.handles.values():
            self.overlay_api.hideOverlay(handle)
        self.visible = False
        self.pending_hide_at = None
        self.input_feedback_hide_at = None

    def toggle_visible(self):
        if not self.has_result:
            return
        self.hide_all() if self.visible else self.show_all()

    def render_all(self):
        with self.state_lock:
            snapshot = dict(self.state)
            snapshot["replies"] = list(self.state.get("replies", []))
        images = {"context": context_image(snapshot)}
        for index in range(3):
            images[f"reply{index}"] = reply_image(index, snapshot)
        images["close"] = close_image(snapshot)
        for key, image in images.items():
            self._set_overlay_image(key, image)
        self._set_overlay_image("dashboard", dashboard_image(self.bindings, self.dashboard_saved, self.dashboard_picker_key))
        self._set_overlay_image("dashboard_thumbnail", dashboard_thumbnail())

    def render_dashboard(self):
        """Refresh only the SteamVR settings page, leaving the HUD textures alone."""
        self._set_overlay_image("dashboard", dashboard_image(self.bindings, self.dashboard_saved, self.dashboard_picker_key))
        self._set_overlay_image("dashboard_thumbnail", dashboard_thumbnail())

    def _set_overlay_image(self, key: str, image: Image.Image):
        """Use a fresh filename because SteamVR may still hold the previous PNG."""
        self.render_serial += 1
        path = self.temp_dir / f"{key}_{self.render_serial:06d}.png"
        image.save(path)
        if key in self.handles:
            handle = self.handles[key]
        elif key == "dashboard":
            handle = self.dashboard_handle
        else:
            handle = self.dashboard_thumbnail_handle
        self.paths[key] = path
        self.overlay_api.setOverlayFromFile(handle, str(path))

    def render_reply_overlay(self, index: int):
        """Update one reply card without replacing every HUD texture."""
        key = f"reply{index}"
        with self.state_lock:
            snapshot = dict(self.state)
            snapshot["replies"] = list(self.state.get("replies", []))
        image = reply_image(index, snapshot)
        self._set_overlay_image(key, image)

    def refresh_bindings_from_disk(self):
        """Pick up desktop-setting changes without restarting the overlay."""
        loaded = load_bindings()
        if loaded == self.bindings:
            return
        previous_enabled = self.recognition_enabled
        self.bindings = loaded
        self.state["bindings"] = self.bindings
        self.recognition_enabled = loaded.get("recognition_enabled", "on") == "on"
        logging.info("Controls refreshed before recognition: %s", self.bindings)
        if previous_enabled and not self.recognition_enabled:
            self.close_recognition(feedback=False)
        self.render_dashboard()

    @staticmethod
    def capture_vrchat_window() -> tuple[Image.Image | None, str]:
        """Capture the VRChat client without focusing or moving the window.

        A desktop-wide grab can capture QQ, SteamVR settings, or another app
        sitting in front of VRChat. PrintWindow asks VRChat to render its own
        client surface into an off-screen bitmap, so the AI sees the actual
        VRChat/VR-view scene even when another desktop window is foreground.
        """
        if os.name != "nt":
            return None, "non-windows"
        try:
            import win32gui
            import win32ui

            hwnd = win32gui.FindWindow(None, "VRChat")
            if not hwnd:
                return None, "VRChat window not found"
            left, top, right, bottom = win32gui.GetClientRect(hwnd)
            width, height = right - left, bottom - top
            if width < 64 or height < 64:
                return None, f"VRChat window too small: {width}x{height}"
            hwnd_dc = win32gui.GetWindowDC(hwnd)
            src_dc = win32ui.CreateDCFromHandle(hwnd_dc)
            dst_dc = src_dc.CreateCompatibleDC()
            bitmap = win32ui.CreateBitmap()
            try:
                bitmap.CreateCompatibleBitmap(src_dc, width, height)
                dst_dc.SelectObject(bitmap)
                ok = ctypes.windll.user32.PrintWindow(hwnd, dst_dc.GetSafeHdc(), 2)
                if not ok:
                    return None, "PrintWindow returned false"
                bits = bitmap.GetBitmapBits(True)
                image = Image.frombuffer("RGB", (width, height), bits, "raw", "BGRX", 0, 1).copy()
                return image, f"VRChat window {width}x{height}"
            finally:
                win32gui.DeleteObject(bitmap.GetHandle())
                dst_dc.DeleteDC()
                src_dc.DeleteDC()
                win32gui.ReleaseDC(hwnd, hwnd_dc)
        except Exception as exc:
            logging.exception("VRChat window capture failed")
            return None, str(exc)

    def cycle_binding(self, action: str):
        current = self.bindings.get(action, BINDING_DEFAULTS[action])
        choices = CONFIRM_CHOICES if action == "confirm_reply" else DASHBOARD_CHOICES
        try:
            index = choices.index(current)
        except ValueError:
            index = -1
        self.bindings[action] = choices[(index + 1) % len(choices)]
        self.state["bindings"] = self.bindings
        self.dashboard_saved = False
        logging.info("Dashboard setting changed key=%s value=%s", action, self.bindings[action])
        self.render_dashboard()

    def cycle_option(self, key: str):
        choices = {
            "hud_visibility": ["auto", "always_hidden"],
            "text_output": ["keyboard", "tapi_osc"],
            "clipboard_capture": ["off", "on"],
            "recognition_enabled": ["on", "off"],
        }[key]
        current = self.bindings.get(key, choices[0])
        try:
            index = choices.index(current)
        except ValueError:
            index = -1
        self.bindings[key] = choices[(index + 1) % len(choices)]
        if key == "recognition_enabled":
            self.recognition_enabled = self.bindings[key] == "on"
            # The master switch must take effect immediately, even before the
            # user presses the optional Save button.
            if not self.recognition_enabled:
                self.close_recognition(feedback=False)
            CONTROL_PATH.write_text(json.dumps(self.bindings, ensure_ascii=False, indent=2), encoding="utf-8")
            logging.info("Recognition enabled changed immediately: %s", self.recognition_enabled)
        self.state["bindings"] = self.bindings
        self.dashboard_saved = False
        logging.info("Dashboard setting changed key=%s value=%s", key, self.bindings[key])
        self.render_dashboard()

    def save_bindings(self):
        CONTROL_PATH.write_text(json.dumps(self.bindings, ensure_ascii=False, indent=2), encoding="utf-8")
        self.dashboard_saved = True
        logging.info("Dashboard settings saved: %s", self.bindings)
        self.render_dashboard()

    @staticmethod
    def dashboard_mouse_coordinates(raw_x: float, raw_y: float) -> tuple[float, float]:
        """Convert SteamVR overlay mouse coordinates to our top-left image space.

        OpenVR reports overlay mouse ``y`` from the bottom edge. The dashboard
        PNG and all hit rectangles in this module use the usual top-left origin.
        SteamVR may report either scaled pixels or normalized 0..1 coordinates,
        so normalize both forms before flipping the vertical axis.
        """
        x = float(raw_x)
        y = float(raw_y)
        if abs(x) <= 1.5:
            x *= DASHBOARD_SIZE[0]
        if abs(y) <= 1.5:
            y *= DASHBOARD_SIZE[1]
        x = max(0.0, min(float(DASHBOARD_SIZE[0]), x))
        y = max(0.0, min(float(DASHBOARD_SIZE[1]), float(DASHBOARD_SIZE[1]) - y))
        return x, y

    def handle_dashboard_event(self, event):
        raw_x = float(event.data.mouse.x)
        raw_y = float(event.data.mouse.y)
        logging.info(
            "Dashboard event type=%s raw_mouse=(%s,%s)",
            int(event.eventType),
            raw_x,
            raw_y,
        )
        if event.eventType != openvr.VREvent_MouseButtonDown:
            return
        now = time.monotonic()
        if now - self.last_dashboard_click_at < 0.15:
            return
        self.last_dashboard_click_at = now
        x, y = self.dashboard_mouse_coordinates(raw_x, raw_y)
        logging.info("Dashboard click mapped_mouse=(%.1f,%.1f)", x, y)
        signature = (round(x), round(y))
        # SteamVR can deliver one dashboard click both on the dashboard queue
        # and on the global queue. Treat the second copy as the same click.
        if signature == self.last_dashboard_click_signature and now - self.last_dashboard_click_at < 0.45:
            logging.info("Duplicate dashboard click ignored x=%.1f y=%.1f", x, y)
            return
        self.last_dashboard_click_signature = signature
        rows = [
            (190, 268, "translate", True),
            (280, 358, "confirm_reply", True),
            (370, 448, "recognition_enabled", False),
            (460, 538, "hud_visibility", False),
            (550, 628, "text_output", False),
            (640, 718, "clipboard_capture", False),
        ]
        if self.dashboard_picker_key:
            choices = dashboard_candidates(self.dashboard_picker_key)
            if 1208 <= x <= 1508:
                for index, choice in enumerate(choices[:6]):
                    top = 286 + index * 82
                    if top <= y <= top + 62:
                        key = self.dashboard_picker_key
                        self.bindings[key] = choice
                        if key == "recognition_enabled":
                            self.recognition_enabled = choice == "on"
                            if not self.recognition_enabled:
                                self.close_recognition(feedback=False)
                            CONTROL_PATH.write_text(json.dumps(self.bindings, ensure_ascii=False, indent=2), encoding="utf-8")
                        self.dashboard_picker_key = None
                        self.dashboard_saved = False
                        logging.info("Dashboard candidate selected key=%s value=%s", key, choice)
                        self.render_dashboard()
                        return
            # A click outside a candidate closes the picker without changing it.
            self.dashboard_picker_key = None
            self.render_dashboard()
            return
        for y0, y1, key, is_binding in rows:
            if y0 <= y < y1:
                self.dashboard_picker_key = key
                logging.info("Dashboard candidate picker opened key=%s x=%.1f y=%.1f", key, x, y)
                self.render_dashboard()
                return
        if x >= 1210 and y >= 870:
            self.save_bindings()
            logging.info("Dashboard save click handled x=%.1f y=%.1f", x, y)

    def recognize_async(self):
        self.refresh_bindings_from_disk()
        if not self.recognition_enabled:
            logging.info("Recognition request ignored because recognition is disabled")
            return
        with self.state_lock:
            if self.state["busy"]:
                logging.info("Recognition request ignored while busy")
                return
            self.state["busy"] = True
            self.state["detected"] = "识别中…"
            self.state["meaning"] = "AI 正在分析当前画面"
            self.state["replies"] = []
        self.has_result = False
        self.input_feedback_hide_at = None
        self.hide_all()
        self.hovered_reply = None
        self.hover_candidate = None
        self.selected_reply = 0
        self.joystick_selected_until = 0.0
        self.render_all()
        self.show_context_only()
        logging.info("Recognition started")

        def worker():
            try:
                if self.bindings.get("clipboard_capture", "off") == "on":
                    image = ImageGrab.grab(all_screens=True)
                    clipboard = self.get_clipboard_text().strip()
                    if clipboard:
                        image = clipboard_image(clipboard)
                        logging.info("Recognition source=clipboard chars=%s", len(clipboard))
                    else:
                        window_image, window_detail = self.capture_vrchat_window()
                        image = window_image or ImageGrab.grab(all_screens=True)
                        logging.info("Clipboard capture enabled but clipboard was empty; source=%s", window_detail)
                else:
                    image, window_detail = self.capture_vrchat_window()
                    if image is None:
                        image = ImageGrab.grab(all_screens=True)
                        logging.info("Recognition source=desktop_fallback reason=%s", window_detail)
                    else:
                        logging.info("Recognition source=vrchat_window detail=%s", window_detail)
                result = ask_model(load_settings(), image)
                self.result_queue.put(("success", result))
            except Exception as exc:
                logging.exception("AI recognition failed")
                self.result_queue.put(("error", exc))

        threading.Thread(target=worker, daemon=True).start()

    def choose_reply(self, index: int):
        with self.state_lock:
            replies = list(self.state.get("replies", []))
        if index >= len(replies):
            return
        text = clean_reply(str(replies[index].get("text", "")))
        if not text:
            return
        try:
            self.set_clipboard_text(text)
        except Exception:
            logging.exception("Could not copy reply text to clipboard")
        self.has_result = False
        with self.state_lock:
            self.state["pressed_reply"] = index
        self.render_reply_overlay(index)
        self.feedback(index, pressed=True)
        # Leave the pressed state visible briefly so the selection is legible.
        self.pending_paste_text = text
        self.pending_hide_at = time.monotonic() + 0.18

    @staticmethod
    def _osc_string(value: str) -> bytes:
        raw = value.encode("utf-8") + b"\0"
        return raw + (b"\0" * ((4 - len(raw) % 4) % 4))

    @staticmethod
    def set_clipboard_text(text: str):
        """Write Unicode text without leaving a Tk window/focus behind."""
        if os.name != "nt":
            return
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.EmptyClipboard.restype = wintypes.BOOL
        user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
        user32.SetClipboardData.restype = ctypes.c_void_p
        user32.CloseClipboard.restype = wintypes.BOOL
        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.restype = wintypes.BOOL
        kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
        kernel32.GlobalFree.restype = ctypes.c_void_p
        data = (text + "\0").encode("utf-16-le")
        if not user32.OpenClipboard(None):
            raise RuntimeError("无法打开剪贴板")
        handle = None
        try:
            user32.EmptyClipboard()
            handle = kernel32.GlobalAlloc(0x0042, len(data))
            if not handle:
                raise RuntimeError("无法分配剪贴板内存")
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                raise RuntimeError("无法锁定剪贴板内存")
            ctypes.memmove(pointer, data, len(data))
            kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(13, handle):
                raise RuntimeError("无法写入剪贴板")
            handle = None
        finally:
            if handle:
                kernel32.GlobalFree(handle)
            user32.CloseClipboard()

    @staticmethod
    def get_clipboard_text() -> str:
        if os.name != "nt":
            return ""
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = ctypes.c_void_p
        user32.CloseClipboard.restype = wintypes.BOOL
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.restype = wintypes.BOOL
        if not user32.OpenClipboard(None):
            return ""
        pointer = None
        try:
            handle = user32.GetClipboardData(13)
            if not handle:
                return ""
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                return ""
            return ctypes.wstring_at(pointer)
        finally:
            if pointer:
                kernel32.GlobalUnlock(handle)
            user32.CloseClipboard()

    def send_chatbox_osc(self, text: str, send: bool):
        """Send VRChat Chatbox OSC, compatible with Tapi and other OSC tools."""
        try:
            # VRChat's current endpoint uses OSC boolean tags with no payload:
            # s + send-now + notification-sound.
            text = self.normalize_chatbox_text(text)
            tags = ",sTT" if send else ",sFT"
            packet = self._osc_string("/chatbox/input") + self._osc_string(tags)
            packet += self._osc_string(text)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.sendto(packet, ("127.0.0.1", 9000))
            logging.info("VRChat OSC chatbox sent=%s bytes=%s text=%r", send, len(packet), text[:80])
        except Exception:
            logging.exception("VRChat OSC chatbox send failed")

    @staticmethod
    def normalize_chatbox_text(text: str) -> str:
        """Match VRChat Chatbox limits before sending UTF-8 text."""
        lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
        return "\n".join(lines[:9])[:144]

    def paste_into_vrchat(self, text: str):
        """Paste the selected target-language text into the VRChat window."""
        if not text or os.name != "nt":
            return
        if self.bindings.get("text_output", "keyboard") == "tapi_osc":
            self.send_chatbox_osc(text, send=True)
            return
        try:
            self.set_clipboard_text(text)
            user32 = ctypes.windll.user32
            hwnd = user32.FindWindowW(None, "VRChat")
            if hwnd:
                user32.ShowWindow(hwnd, 5)
                user32.SetForegroundWindow(hwnd)
                time.sleep(0.06)
            # The clipboard already contains the clean target-language body.
            # SendInput is used instead of shelling out so no extra process can
            # steal focus or interfere with SteamVR.
            user32.keybd_event(0x11, 0, 0, 0)       # CTRL down
            user32.keybd_event(0x56, 0, 0, 0)       # V down
            user32.keybd_event(0x56, 0, 2, 0)       # V up
            user32.keybd_event(0x11, 0, 2, 0)       # CTRL up
            # VRChat submits the focused chatbox with Enter. Keep this after
            # paste so only the target-language body is sent.
            time.sleep(0.08)
            user32.keybd_event(0x0D, 0, 0, 0)       # ENTER down
            user32.keybd_event(0x0D, 0, 2, 0)       # ENTER up
            logging.info("Reply pasted and submitted to VRChat")
        except Exception:
            logging.exception("Reply paste failed; text remains in clipboard")

    def feedback(self, index, pressed: bool = False):
        """Give laser/controller feedback without affecting VRChat or SteamVR."""
        handle_key = "close" if index == "close" else f"reply{index}"
        handle = self.handles.get(handle_key)
        if handle is not None:
            try:
                self.overlay_api.triggerLaserMouseHapticVibration(
                    handle,
                    0.08 if pressed else 0.035,
                    150.0 if pressed else 110.0,
                    0.9 if pressed else 0.45,
                )
            except Exception:
                pass
        if self.last_controller_device is not None and self.vr_system is not None:
            try:
                self.vr_system.triggerHapticPulse(
                    self.last_controller_device,
                    0,
                    120 if pressed else 70,
                )
            except Exception:
                pass

    def apply_worker_results(self):
        """Apply AI results on the OpenVR loop thread; OpenVR calls stay serialized."""
        changed = False
        while True:
            try:
                kind, payload = self.result_queue.get_nowait()
            except queue.Empty:
                break
            changed = True
            with self.state_lock:
                if kind == "success":
                    result = payload if isinstance(payload, dict) else {}
                    self.state["detected"] = f"{result.get('detected_text', '未识别到文字')} · {result.get('detected_language', '')}"
                    self.state["meaning"] = result.get("meaning_zh", "未能生成中文含义")
                    self.state["replies"] = result.get("replies", [])[:3]
                else:
                    self.state["detected"] = "识别失败"
                    self.state["meaning"] = str(payload)
                    self.state["replies"] = []
                self.state["busy"] = False
            self.has_result = True
            self.show_all()
            logging.info("Recognition result applied (%s)", kind)
        if changed:
            self.render_all()

    def poll_keyboard_fallback(self):
        """F8 is a non-focus-stealing recognition fallback."""
        if os.name != "nt" or self.input_ready:
            return
        try:
            user32 = ctypes.windll.user32
            if self.hotkeys_registered:
                msg = wintypes.MSG()
                while user32.PeekMessageW(ctypes.byref(msg), None, 0x0312, 0x0312, 1):
                    if msg.wParam == 1:
                        self.recognize_async()
                return
            for vk, action in ((0x77, "translate"),):
                down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
                if down and not self.keyboard_state[vk]:
                    if action == "translate":
                        self.recognize_async()
                self.keyboard_state[vk] = down
        except Exception:
            pass

    def register_fallback_hotkeys(self):
        if os.name != "nt":
            return
        try:
            user32 = ctypes.windll.user32
            first = bool(user32.RegisterHotKey(None, 1, 0, 0x77))
            self.hotkeys_registered = first
            logging.info("Fallback hotkey registered F8=%s", first)
        except Exception:
            logging.exception("Fallback hotkey registration failed")

    @staticmethod
    def _binding_matches(binding: str, role: str) -> tuple[bool, str]:
        if binding == "trigger":
            return True, "trigger"
        parts = binding.split("_", 1)
        if len(parts) != 2:
            return False, binding
        return parts[0] == role, parts[1]

    def handle_button_press(self, device_index: int, button: int):
        try:
            role_value = self.vr_system.getControllerRoleForTrackedDeviceIndex(device_index)
        except Exception:
            logging.exception("Could not read controller role for device %s", device_index)
            return
        role = "left" if role_value == openvr.TrackedControllerRole_LeftHand else "right" if role_value == openvr.TrackedControllerRole_RightHand else ""
        if not role:
            role = self.resolve_controller_roles().get(device_index, "")
        if not role:
            return
        if not self.recognition_enabled:
            logging.info("Legacy button press ignored while recognition is disabled role=%s button=%s", role, button)
            return
        self.last_controller_device = device_index
        logging.info("Controller button press role=%s button=%s", role, button)
        for action, binding in self.bindings.items():
            if action not in BINDING_DEFAULTS:
                continue
            matches, button_name = self._binding_matches(binding, role)
            if not matches or button_name not in BUTTON_CODES or BUTTON_CODES[button_name] != button:
                continue
            if action == "toggle_visibility":
                self.toggle_visible()
            elif action == "translate":
                if not self.digital_inputs_armed:
                    logging.info("Ignored legacy recognition press before trigger input was armed")
                    continue
                self.button_press_started[f"legacy:{device_index}:{action}"] = time.monotonic()
            elif action == "confirm_reply" and self.visible:
                if self.selected_reply < 3:
                    self.choose_reply(self.selected_reply)
                else:
                    self.close_recognition()

    def handle_button_release(self, device_index: int, button: int):
        try:
            role_value = self.vr_system.getControllerRoleForTrackedDeviceIndex(device_index)
        except Exception:
            return
        role = "left" if role_value == openvr.TrackedControllerRole_LeftHand else "right" if role_value == openvr.TrackedControllerRole_RightHand else ""
        if not role:
            role = self.resolve_controller_roles().get(device_index, "")
        if not role:
            return
        if not self.recognition_enabled:
            self.button_press_started.clear()
            return
        for action, binding in self.bindings.items():
            if action != "translate":
                continue
            matches, button_name = self._binding_matches(binding, role)
            if not matches or button_name not in BUTTON_CODES or BUTTON_CODES[button_name] != button:
                continue
            key = f"legacy:{device_index}:{action}"
            started = self.button_press_started.pop(key, None)
            if started is None:
                continue
            if not self.digital_inputs_armed:
                continue
            duration = time.monotonic() - started
            if duration < LONG_PRESS_SECONDS:
                self.recognize_async()
            logging.info("Legacy trigger released role=%s duration=%.2f long=%s", role, duration, duration >= LONG_PRESS_SECONDS)

    def poll_controller_events(self):
        """Read SteamVR Input actions; this works for overlay apps where legacy state is denied."""
        self.action_trigger_active = False
        self.action_joystick_active = False
        action_path_usable = False
        if self.input_ready:
            try:
                active_sets = (openvr.VRActiveActionSet_t * 1)()
                active_sets[0].ulActionSet = self.action_set_handle
                active_sets[0].ulRestrictedToDevice = openvr.k_ulInvalidInputValueHandle
                active_sets[0].ulSecondaryActionSet = openvr.k_ulInvalidActionSetHandle
                active_sets[0].nPriority = 0
                self.vr_input.updateActionState(active_sets)
                digital_ready = self.digital_inputs_armed
                if not digital_ready:
                    trigger_states = []
                    for name in ("left_trigger", "right_trigger"):
                        handle = self.action_handles.get(name)
                        if handle:
                            trigger_data = self.vr_input.getDigitalActionData(handle, openvr.k_ulInvalidInputValueHandle)
                            action_path_usable = action_path_usable or bool(getattr(trigger_data, "bActive", False))
                            trigger_states.append(bool(trigger_data.bState))
                    if trigger_states and not any(trigger_states):
                        self.digital_inputs_armed = True
                        digital_ready = True
                        logging.info("Controller trigger input armed after release")
                    else:
                        self.button_press_started.clear()
                        self.long_press_fired.clear()
                if digital_ready:
                    if not self.recognition_enabled:
                        # Keep SteamVR Input alive so the dashboard still works,
                        # but discard all pending HUD/controller interactions.
                        self.button_press_started.clear()
                        self.long_press_fired.clear()
                    for action, binding in self.bindings.items():
                        if action not in BINDING_DEFAULTS:
                            continue
                        physical = [binding]
                        if binding == "trigger":
                            physical = ["left_trigger", "right_trigger"]
                        for name in physical:
                            handle = self.action_handles.get(name)
                            if not handle:
                                continue
                            data = self.vr_input.getDigitalActionData(handle, openvr.k_ulInvalidInputValueHandle)
                            if getattr(data, "bActive", False):
                                self.action_trigger_active = True
                                action_path_usable = True
                            if not self.recognition_enabled:
                                continue
                            if action == "translate" and (bool(getattr(data, "bState", False)) or bool(getattr(data, "bChanged", False))):
                                self.action_digital_event_seen = True
                            now = time.monotonic()
                            press_key = f"{action}:{name}"
                            if bool(data.bState) and press_key not in self.button_press_started:
                                self.button_press_started[press_key] = now
                                logging.info("Digital action pressed action=%s physical=%s", action, name)
                                if action == "translate" and self.recognition_enabled:
                                    self.show_input_feedback("准备识别…", "松开右扳机开始 AI 识别", duration=1.2)
                                else:
                                    self.handle_bound_action(action)
                            if bool(data.bState) and action == "translate":
                                if now - self.button_press_started[press_key] >= LONG_PRESS_SECONDS:
                                    if press_key not in self.long_press_fired:
                                        self.long_press_fired.add(press_key)
                                        self.disable_recognition()
                            elif not bool(data.bState) and press_key in self.button_press_started:
                                started = self.button_press_started.pop(press_key, now)
                                was_long = press_key in self.long_press_fired
                                self.long_press_fired.discard(press_key)
                                logging.info("Digital action released action=%s physical=%s duration=%.2f long=%s", action, name, now - started, was_long)
                                if action == "translate" and not was_long:
                                    self.recognize_async()
                # Selection is deliberately bound to the right stick so the
                # left stick remains free for normal VRChat movement.
                analog_y = 0.0
                right_handle = self.analog_handles.get("right_joystick")
                if right_handle:
                    data = self.vr_input.getAnalogActionData(right_handle, openvr.k_ulInvalidInputValueHandle)
                    self.action_joystick_active = bool(getattr(data, "bActive", False))
                    analog_y = float(data.y)
                    if abs(analog_y) >= 0.2 or abs(float(getattr(data, "deltaY", 0.0))) >= 0.2:
                        self.action_joystick_event_seen = True
                now = time.monotonic()
                if self.recognition_enabled and self.action_joystick_active and abs(analog_y) >= JOYSTICK_DEADZONE and now >= self.next_joystick_move_at:
                    self.move_selection(-1 if analog_y > 0 else 1)
                    self.next_joystick_move_at = now + 0.28
            except Exception:
                logging.exception("SteamVR Input action polling failed")

        # Virtual Desktop's vd_hand_controller exposes reliable SteamVR Input
        # actions but often reports a permanently idle legacy controller state.
        # Only use the raw fallback when the action path is unavailable.
        if not action_path_usable:
            self.poll_legacy_controller_state(
                trigger_fallback=True,
                joystick_fallback=True,
                confirm_fallback=True,
            )

        # Keep the legacy event queue as a compatibility path for older runtimes.
        event = openvr.VREvent_t()
        while self.vr_system.pollNextEvent(event):
            if event.eventType == openvr.VREvent_ButtonPress:
                try:
                    self.handle_button_press(event.trackedDeviceIndex, int(event.data.controller.button))
                except Exception:
                    logging.exception("Legacy controller button event failed")
            elif event.eventType == openvr.VREvent_ButtonUnpress:
                try:
                    self.handle_button_release(event.trackedDeviceIndex, int(event.data.controller.button))
                except Exception:
                    logging.exception("Legacy controller button release failed")

    def close_recognition(self, feedback: bool = False):
        """Clear the current recognition HUD, optionally showing close feedback."""
        self.has_result = False
        with self.state_lock:
            self.state["detected"] = ""
            self.state["meaning"] = ""
            self.state["replies"] = []
            self.state["pressed_reply"] = None
            self.state["hover_reply"] = None
        if feedback:
            self.show_input_feedback("已关闭本次识别", "右扳机长按已生效，HUD 即将收起", duration=0.55)
        else:
            self.hide_all()
        logging.info("Recognition HUD closed by long press feedback=%s", feedback)

    def disable_recognition(self):
        """Close the current HUD and lock recognition until settings re-enable it."""
        self.recognition_enabled = False
        self.bindings["recognition_enabled"] = "off"
        try:
            CONTROL_PATH.write_text(json.dumps(self.bindings, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            logging.exception("Could not persist recognition disabled state")
        # Once the master switch is off, no HUD card or input feedback may be
        # shown. The SteamVR dashboard remains available for re-enabling it.
        self.close_recognition(feedback=False)
        self.render_dashboard()
        logging.info("Recognition disabled by right-trigger long press")

    def handle_bound_action(self, action: str):
        if not self.recognition_enabled:
            logging.info("Bound action ignored while recognition is disabled: %s", action)
            return
        if action == "translate":
            self.recognize_async()
        elif action == "confirm_reply" and self.visible:
            if self.selected_reply < 3:
                self.choose_reply(self.selected_reply)
            else:
                self.close_recognition()

    def resolve_controller_roles(self) -> dict[int, str]:
        """Resolve real left/right controllers even when OpenVR reports role 0.

        Some Oculus/VirtualDesktop runtimes return ``TrackedControllerRole_Invalid``
        from ``getControllerRoleForTrackedDeviceIndex`` while still publishing the
        stable controller-role property.  Prefer that property and prefer physical
        Oculus devices over VirtualDesktop's synthetic hand devices.
        """
        candidates: dict[str, list[tuple[int, int]]] = {"left": [], "right": []}
        max_devices = int(getattr(openvr, "k_unMaxTrackedDeviceCount", 16))
        for device_index in range(max_devices):
            try:
                if self.vr_system.getTrackedDeviceClass(device_index) != openvr.TrackedDeviceClass_Controller:
                    continue
                role_value = self.vr_system.getControllerRoleForTrackedDeviceIndex(device_index)
            except Exception:
                continue
            role = ""
            score = 0
            if role_value == openvr.TrackedControllerRole_LeftHand:
                role, score = "left", 100
            elif role_value == openvr.TrackedControllerRole_RightHand:
                role, score = "right", 100
            try:
                role_hint = self.vr_system.getInt32TrackedDeviceProperty(
                    device_index, openvr.Prop_ControllerRoleHint_Int32
                )
                if role_hint == openvr.TrackedControllerRole_LeftHand and score < 100:
                    role, score = "left", 90
                elif role_hint == openvr.TrackedControllerRole_RightHand and score < 100:
                    role, score = "right", 90
            except Exception:
                role_hint = 0
            try:
                serial = self.vr_system.getStringTrackedDeviceProperty(
                    device_index, openvr.Prop_SerialNumber_String
                )
            except Exception:
                serial = ""
            try:
                tracking = self.vr_system.getStringTrackedDeviceProperty(
                    device_index, openvr.Prop_TrackingSystemName_String
                )
            except Exception:
                tracking = ""
            serial_lower = serial.lower()
            # Explicit device naming is more reliable than the role API on
            # Oculus/VirtualDesktop, which can swap or return Invalid at runtime.
            if serial_lower.endswith("_left") or "_controller_left" in serial_lower or serial_lower == "handl":
                role, score = "left", 220
            elif serial_lower.endswith("_right") or "_controller_right" in serial_lower or serial_lower == "handr":
                role, score = "right", 220
            # Real tracked controllers win over VirtualDesktop synthetic hands.
            if tracking.lower() != "virtualdesktop":
                score += 20
            if "controller_" in serial_lower:
                score += 10
            if role:
                candidates[role].append((score, device_index))

        roles: dict[int, str] = {}
        for role, items in candidates.items():
            if items:
                _, device_index = max(items, key=lambda item: (item[0], -item[1]))
                roles[device_index] = role

        # If a runtime exposes no role hint at all, retain the last known pairing
        # instead of swapping left/right merely because device indices changed.
        if not roles:
            available = {
                device_index
                for device_index in range(max_devices)
                if self.vr_system.getTrackedDeviceClass(device_index) == openvr.TrackedDeviceClass_Controller
            }
            for device_index, role in self.fallback_controller_roles.items():
                if device_index in available and role not in roles.values():
                    roles[device_index] = role
            for device_index in sorted(available - set(roles)):
                if "left" not in roles.values():
                    roles[device_index] = "left"
                elif "right" not in roles.values():
                    roles[device_index] = "right"
        if roles != self.fallback_controller_roles:
            self.fallback_controller_roles = roles
            logging.info("Controller role map=%s", roles)
        return roles

    def poll_legacy_controller_state(self, *, trigger_fallback: bool, joystick_fallback: bool, confirm_fallback: bool = True):
        """Read raw controller state for reliable trigger/joystick controls."""
        if not self.vr_system or not hasattr(self.vr_system, "getControllerState"):
            return False
        seen_roles: set[str] = set()
        controller_seen = False
        arming = not self.digital_inputs_armed
        observed_trigger_states: dict[str, bool] = {}
        try:
            trigger_mask = 1 << int(BUTTON_CODES["trigger"])
            for device_index, role in self.resolve_controller_roles().items():
                try:
                    ok, state = self.vr_system.getControllerState(device_index)
                except Exception:
                    continue
                if not ok:
                    continue
                controller_seen = True
                self.last_controller_device = device_index
                seen_roles.add(role)
                trigger_axis = 0.0
                axis_snapshot = []
                try:
                    axis_snapshot = [
                        (round(float(state.rAxis[index].x), 3), round(float(state.rAxis[index].y), 3))
                        for index in range(5)
                    ]
                    # Oculus Touch exposes the analog trigger on axis 1.x. A
                    # few runtimes put it on axis 1.y, so accept either positive
                    # component without treating the thumbstick as a trigger.
                    trigger_axis = max(0.0, float(state.rAxis[1].x), float(state.rAxis[1].y))
                except Exception:
                    pass
                signature = (int(state.ulButtonPressed), tuple(axis_snapshot))
                state_signatures = getattr(self, "controller_state_signatures", None)
                if state_signatures is None:
                    state_signatures = self.controller_state_signatures = {}
                if state_signatures.get(device_index) != signature:
                    state_signatures[device_index] = signature
                    logging.info(
                        "Raw controller state device=%s role=%s buttons=%s axes=%s",
                        device_index,
                        role,
                        int(state.ulButtonPressed),
                        axis_snapshot,
                    )
                trigger_down = bool(int(state.ulButtonPressed) & trigger_mask) or trigger_axis >= TRIGGER_AXIS_THRESHOLD
                if arming:
                    observed_trigger_states[role] = trigger_down
                    self.legacy_trigger_states[role] = trigger_down
                    continue
                if not self.recognition_enabled:
                    self.legacy_trigger_started.pop(role, None)
                    self.legacy_long_press_fired.discard(role)
                    self.legacy_trigger_states[role] = trigger_down
                    continue
                if trigger_fallback:
                    previous = self.legacy_trigger_states.get(role, False)
                    now = time.monotonic()
                    translate_match, _ = self._binding_matches(self.bindings.get("translate", "right_trigger"), role)
                    confirm_match, _ = self._binding_matches(self.bindings.get("confirm_reply", "left_trigger"), role)
                    if role == "right" and translate_match:
                        if trigger_down and not previous:
                            self.legacy_trigger_started[role] = now
                            logging.info("Legacy controller trigger pressed role=%s axis=%.2f", role, trigger_axis)
                            self.show_input_feedback("准备识别…", "松开右扳机开始 AI 识别", duration=1.2)
                        if trigger_down:
                            started = self.legacy_trigger_started.get(role, now)
                            if now - started >= LONG_PRESS_SECONDS and role not in self.legacy_long_press_fired:
                                self.legacy_long_press_fired.add(role)
                                self.disable_recognition()
                                logging.info("Legacy controller trigger long press role=%s", role)
                        elif previous:
                            started = self.legacy_trigger_started.pop(role, now)
                            was_long = role in self.legacy_long_press_fired
                            self.legacy_long_press_fired.discard(role)
                            logging.info("Legacy controller trigger released role=%s duration=%.2f long=%s", role, now - started, was_long)
                            if not was_long:
                                self.recognize_async()
                    elif role == "left" and confirm_match and confirm_fallback and trigger_down and not previous:
                        logging.info("Legacy controller confirm pressed role=left selected=%s", self.selected_reply)
                        self.handle_bound_action("confirm_reply")
                    self.legacy_trigger_states[role] = trigger_down
                if joystick_fallback and role == "right":
                    try:
                        analog_y = float(state.rAxis[0].y)
                    except Exception:
                        analog_y = 0.0
                    now = time.monotonic()
                    if abs(analog_y) >= JOYSTICK_DEADZONE and now >= self.next_joystick_move_at:
                        self.move_selection(-1 if analog_y > 0 else 1)
                        self.next_joystick_move_at = now + 0.28
                        logging.info(
                            "Legacy right joystick y=%.2f selected=%s",
                            analog_y,
                            getattr(self, "selected_reply", None),
                        )
            for role in ("left", "right"):
                if role not in seen_roles:
                    self.legacy_trigger_states[role] = False
            if arming and observed_trigger_states and not any(observed_trigger_states.values()):
                self.digital_inputs_armed = True
                logging.info("Raw controller trigger input armed after release")
            if time.monotonic() - self.last_input_diag_at >= 2.0:
                self.last_input_diag_at = time.monotonic()
                logging.info(
                    "Input diagnostics action_trigger_active=%s action_joystick_active=%s legacy_fallback=%s controller_seen=%s",
                    self.action_trigger_active,
                    self.action_joystick_active,
                    trigger_fallback or joystick_fallback,
                    controller_seen,
                )
            return controller_seen
        except Exception:
            logging.exception("Legacy controller state polling failed")
            return False

    def move_selection(self, delta: int):
        if not self.recognition_enabled or not self.visible:
            return
        previous = self.selected_reply
        self.selected_reply = (self.selected_reply + delta) % SELECTION_COUNT
        target = self.selection_target(self.selected_reply)
        self.hovered_reply = target
        self.hover_candidate = target
        self.hover_candidate_since = time.monotonic()
        self.joystick_selected_until = time.monotonic() + JOYSTICK_SELECTION_HOLD_SECONDS
        with self.state_lock:
            self.state["hover_reply"] = target
        if previous < 3:
            self.render_reply_overlay(previous)
        else:
            self._set_overlay_image("close", close_image(dict(self.state)))
        if self.selected_reply < 3:
            self.render_reply_overlay(self.selected_reply)
        else:
            self._set_overlay_image("close", close_image(dict(self.state)))
        self.feedback(target)

    @staticmethod
    def selection_target(index: int):
        if index < 3:
            return index
        return "close"

    def handle_events(self):
        dashboard_event = openvr.VREvent_t()
        has_event, _ = self.overlay_api.pollNextOverlayEvent(self.dashboard_handle, dashboard_event)
        while has_event:
            self.handle_dashboard_event(dashboard_event)
            has_event, _ = self.overlay_api.pollNextOverlayEvent(self.dashboard_handle, dashboard_event)
        for key, handle in self.handles.items():
            event = openvr.VREvent_t()
            has_event, _ = self.overlay_api.pollNextOverlayEvent(handle, event)
            while has_event:
                has_event, _ = self.overlay_api.pollNextOverlayEvent(handle, event)
        # Some SteamVR runtimes deliver Dashboard mouse clicks through the
        # global event queue instead of the dashboard overlay queue.  Consume
        # those click events too; without this, the panel visibly reacts but
        # none of its settings change.
        global_event = openvr.VREvent_t()
        while self.vr_system.pollNextEvent(global_event):
            if global_event.eventType == openvr.VREvent_MouseButtonDown:
                self.handle_dashboard_event(global_event)
        # HUD selection is controller-only; do not poll laser hover targets.

    def update_hover_target(self):
        """Commit a laser target only after it remains stable for a short interval."""
        if not self.visible:
            return
        now = time.monotonic()
        # Selection is controller-only; laser/cursor targeting is disabled.
        return

    def run(self):
        logging.info("Starting SteamVR HUD")
        PID_PATH.write_text(str(os.getpid()), encoding="ascii")
        try:
            self.setup()
            loop_count = 0
            logging.info("SteamVR HUD ready")
            while self.running:
                if STOP_PATH.exists():
                    self.running = False
                    break
                if loop_count == 0:
                    logging.info("loop before apply_worker_results")
                self.apply_worker_results()
                if self.input_feedback_hide_at is not None and time.monotonic() >= self.input_feedback_hide_at:
                    self.input_feedback_hide_at = None
                    self.hide_all()
                if self.pending_hide_at is not None and time.monotonic() >= self.pending_hide_at:
                    self.pending_hide_at = None
                    with self.state_lock:
                        self.state["pressed_reply"] = None
                    self.hide_all()
                    if self.pending_paste_text:
                        text = self.pending_paste_text
                        self.pending_paste_text = None
                        self.paste_into_vrchat(text)
                if loop_count == 0:
                    logging.info("loop before handle_events")
                self.handle_events()
                if loop_count == 0:
                    logging.info("loop before poll_controller_events")
                self.poll_controller_events()
                if loop_count == 0:
                    logging.info("loop before poll_keyboard_fallback")
                self.poll_keyboard_fallback()
                if loop_count == 0:
                    logging.info("loop after poll_keyboard_fallback")
                time.sleep(0.015)
                loop_count += 1
                if loop_count % 200 == 0:
                    logging.info("SteamVR HUD heartbeat visible=%s result=%s busy=%s", self.visible, self.has_result, self.state.get("busy"))
        except Exception:
            logging.exception("SteamVR HUD stopped during setup or event loop")
            raise
        finally:
            if self.hotkeys_registered and os.name == "nt":
                try:
                    user32 = ctypes.windll.user32
                    user32.UnregisterHotKey(None, 1)
                except Exception:
                    pass
            for handle in self.handles.values():
                try:
                    self.overlay_api.hideOverlay(handle)
                    self.overlay_api.destroyOverlay(handle)
                except Exception:
                    pass
            for handle in (self.dashboard_handle, self.dashboard_thumbnail_handle):
                try:
                    self.overlay_api.hideOverlay(handle)
                    self.overlay_api.destroyOverlay(handle)
                except Exception:
                    pass
            openvr.shutdown()
            try:
                PID_PATH.unlink(missing_ok=True)
            except Exception:
                pass
            logging.info("SteamVR HUD stopped")


if __name__ == "__main__":
    SteamVROverlay().run()
