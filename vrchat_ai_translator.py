from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk
from urllib import request

try:
    from PIL import ImageGrab
except ImportError as exc:  # pragma: no cover
    ImageGrab = None
    PIL_IMPORT_ERROR = exc


CONFIG_PATH = Path(r"E:\Documents\deepseek.txt")
DEFAULT_MODEL = "deepseek-v4-flash-vision-exp"
HOTKEY_VK = 0x77  # F8
GLASS_BG = "#eef2f0"
GLASS_PANEL = "#f6f8f7"
INK = "#111315"
MUTED_INK = "#4e5758"


@dataclass
class Settings:
    endpoint: str
    api_key: str
    model: str


def load_settings() -> Settings:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"找不到配置文件：{CONFIG_PATH}")
    lines = [line.strip() for line in CONFIG_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(lines) < 2:
        raise ValueError("deepseek.txt 至少需要接口地址和 API key 两行")
    endpoint = lines[0].rstrip("/")
    api_key = lines[1]
    model = lines[2] if len(lines) >= 3 else DEFAULT_MODEL
    return Settings(endpoint=endpoint, api_key=api_key, model=model)


def image_to_data_url(image) -> str:
    import io

    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=82, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def clean_reply(text: str) -> str:
    text = text.strip()
    # Remove markdown code fences and common numbering prefixes.
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
    text = re.sub(r"^\s*\d+[.)、]\s*", "", text)
    # Only the text before the first full-width or half-width parenthesis is sent.
    text = re.split(r"[\(（]", text, maxsplit=1)[0].strip()
    return text


def parse_json_payload(raw: str) -> dict:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I | re.S).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, flags=re.S)
        if not match:
            raise ValueError("AI 返回的内容不是有效 JSON")
        return json.loads(match.group(0))


def ask_model(settings: Settings, image) -> dict:
    payload = {
        "model": settings.model,
        "temperature": 0.60,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是 VRChat 和多人在线游戏中的实时聊天翻译助手。"
                    "先读取截图中最重要、最可能需要回应的文字；同时观察附近聊天行、标点、表情、称呼、"
                    "游戏状态和语气，判断这是打招呼、提问、邀请、感谢、道歉、玩笑、组队、交易、战斗协作、"
                    "离开/等待，还是普通闲聊。不要只做逐字翻译，要理解对方在当前游戏聊天场景里真正想表达什么。"
                    "然后生成三条可以直接发到 VRChat Chatbox 的短回复。回复要像真实玩家临场打字：自然、简洁、"
                    "口语化，避免客服腔、论文腔、过度礼貌、长篇解释和不必要的完整句号。"
                    "三条回复应有明确差异：第一条是最稳妥的自然回应；第二条更友好或轻松；第三条在合适时"
                    "提供简短的澄清、行动或替代说法。若截图信息不足，不要编造人物关系、地点、任务、物品、"
                    "时间或对方意图，使用不冒进的通用回应。对方只是打招呼时不要回复成正式自我介绍；对方提问时"
                    "要直接回答或简短说明不确定；对方邀请/组队时要体现接受、拒绝或稍等；对方感谢/道歉时要自然接话。"
                    "回复语言优先跟随对方原文；若有多种语言，以正在对话的目标语言为准；无法判断时使用英语。"
                    "用户母语是中文，所有 translation_zh 都要是准确、自然、简短的中文，帮助用户理解语气。"
                    "回复正文 text 必须只包含目标语言正文，不要包含中文、括号、引号、编号、Markdown、解释、"
                    "动作描述或颜文字说明；中文翻译只能放在 translation_zh。不要在 text 中使用半角或全角括号。"
                    "必须只返回一个合法 JSON 对象，不要 Markdown、代码围栏或前后说明。"
                    '格式：{"detected_text":"原文","meaning_zh":"结合聊天场景的中文含义",'
                    '"detected_language":"语言名",'
                    '"replies":[{"text":"目标语言短回复","translation_zh":"中文翻译"},'
                    '{"text":"目标语言短回复","translation_zh":"中文翻译"},'
                    '{"text":"目标语言短回复","translation_zh":"中文翻译"}]}。'
                ),
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "请把这张截图当作 VRChat 或普通多人游戏的真实聊天现场来分析。"
                            "优先处理最需要回应的对话文字，忽略无关 UI、用户名列表和装饰文字。"
                            "先理解语境，再按规定 JSON 返回三条可直接发送的目标语言回复。"
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": image_to_data_url(image)}},
                ],
            },
        ],
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        settings.endpoint + "/chat/completions",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.api_key}",
        },
        method="POST",
    )
    with request.urlopen(req, timeout=90) as response:
        body = json.loads(response.read().decode("utf-8"))
    content = body["choices"][0]["message"]["content"]
    if isinstance(content, list):
        content = "".join(item.get("text", "") for item in content if isinstance(item, dict))
    return parse_json_payload(content)


def send_to_vrchat(text: str) -> None:
    text = clean_reply(text)
    if not text:
        raise ValueError("回复正文为空")
    if not ctypes.windll.user32.OpenClipboard(None):
        raise RuntimeError("无法打开剪贴板")
    try:
        ctypes.windll.user32.EmptyClipboard()
        # Tk clipboard is more reliable for Unicode; the app window owns it briefly.
    finally:
        ctypes.windll.user32.CloseClipboard()


def enable_windows_acrylic(root: tk.Tk) -> None:
    """Ask Windows for a subtle blurred glass backdrop; safely no-op elsewhere."""
    if os.name != "nt":
        return
    try:
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        if not hwnd:
            hwnd = root.winfo_id()
        class AccentPolicy(ctypes.Structure):
            _fields_ = [
                ("AccentState", ctypes.c_int),
                ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_uint),
                ("AnimationId", ctypes.c_int),
            ]
        class WindowCompositionAttributeData(ctypes.Structure):
            _fields_ = [
                ("Attribute", ctypes.c_int),
                ("Data", ctypes.POINTER(AccentPolicy)),
                ("SizeOfData", ctypes.c_size_t),
            ]
        policy = AccentPolicy(3, 2, 0xDDEEF2F0, 0)
        data = WindowCompositionAttributeData(
            19, ctypes.pointer(policy), ctypes.sizeof(policy)
        )
        ctypes.windll.user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(data))
    except Exception:
        pass


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("VRChat AI 回复助手")
        self.root.geometry("760x620")
        self.root.minsize(640, 520)
        self.root.configure(bg=GLASS_BG)
        self.root.attributes("-alpha", 0.94)
        self.font_family = self._pick_font()
        self.settings: Settings | None = None
        self.busy = False
        self.replies: list[dict] = []
        self.status_var = tk.StringVar(value="准备就绪 · F8 识别当前屏幕")
        self.detected_var = tk.StringVar(value="尚未识别")
        self.meaning_var = tk.StringVar(value="按下按钮后，AI 会读取当前屏幕中的文字")
        self._build_ui()
        self.root.after(120, lambda: enable_windows_acrylic(self.root))
        self.root.bind("<F8>", lambda _event: self.capture_and_analyze())

    @staticmethod
    def _pick_font() -> str:
        families = set(tkfont.families())
        for name in ("Source Han Sans CN", "思源黑体", "Source Han Sans", "Noto Sans CJK SC"):
            if name in families:
                return name
        return "Microsoft YaHei UI"

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TButton", font=(self.font_family, 11), padding=(12, 8))
        style.configure("Header.TLabel", background=GLASS_PANEL, foreground=INK, font=(self.font_family, 20, "bold"))
        style.configure("Muted.TLabel", background=GLASS_PANEL, foreground=MUTED_INK, font=(self.font_family, 10))
        style.configure("Card.TFrame", background=GLASS_PANEL)
        style.configure("Card.TLabel", background=GLASS_PANEL, foreground=INK, font=(self.font_family, 11))

        header = ttk.Frame(self.root, style="Card.TFrame")
        header.pack(fill="x", padx=18, pady=(18, 10))
        ttk.Label(header, text="VRChat AI 回复助手", style="Header.TLabel").pack(anchor="w", padx=18, pady=(16, 2))
        ttk.Label(header, text="按 F8 或点击按钮，识别当前画面并生成三条可发送回复", style="Muted.TLabel").pack(anchor="w", padx=18, pady=(0, 16))

        toolbar = ttk.Frame(self.root, style="Card.TFrame")
        toolbar.pack(fill="x", padx=18, pady=4)
        self.capture_btn = ttk.Button(toolbar, text="识别当前屏幕  ·  F8", command=self.capture_and_analyze)
        self.capture_btn.pack(side="left", padx=14, pady=12)
        self.copy_btn = ttk.Button(toolbar, text="复制选中回复", command=self.copy_selected)
        self.copy_btn.pack(side="left", padx=0, pady=12)
        ttk.Label(toolbar, textvariable=self.status_var, style="Muted.TLabel").pack(side="right", padx=14)

        context = ttk.Frame(self.root, style="Card.TFrame")
        context.pack(fill="x", padx=18, pady=4)
        ttk.Label(context, text="识别内容", style="Card.TLabel").pack(anchor="w", padx=16, pady=(14, 2))
        ttk.Label(context, textvariable=self.detected_var, style="Card.TLabel", wraplength=700).pack(anchor="w", padx=16)
        ttk.Label(context, text="中文含义", style="Card.TLabel").pack(anchor="w", padx=16, pady=(10, 2))
        ttk.Label(context, textvariable=self.meaning_var, style="Card.TLabel", wraplength=700).pack(anchor="w", padx=16, pady=(0, 14))

        replies_frame = ttk.Frame(self.root, style="Card.TFrame")
        replies_frame.pack(fill="both", expand=True, padx=18, pady=4)
        ttk.Label(replies_frame, text="回复选项（点击后会复制正文，并尝试粘贴到 VRChat 聊天框）", style="Card.TLabel").pack(anchor="w", padx=16, pady=(14, 8))
        self.reply_buttons: list[tk.Button] = []
        for i in range(3):
            button = tk.Button(
                replies_frame,
                text=f"{i + 1}. 尚未生成",
                command=lambda idx=i: self.choose_reply(idx),
                anchor="w",
                justify="left",
                wraplength=680,
                font=(self.font_family, 12),
                fg=INK,
                bg=GLASS_BG,
                activeforeground=INK,
                activebackground="#ffffff",
                relief="solid",
                bd=1,
                highlightthickness=1,
                highlightbackground="#000000",
                highlightcolor="#000000",
                padx=16,
                pady=12,
                cursor="hand2",
            )
            button.pack(fill="x", padx=16, pady=5)
            self.reply_buttons.append(button)
        ttk.Label(
            self.root,
            text="发送规则：只发送目标语言正文，括号及括号内中文翻译不会发送。请先让 VRChat 聊天输入框获得焦点。",
            style="Muted.TLabel",
            wraplength=740,
        ).pack(fill="x", padx=18, pady=(8, 14))

    def set_busy(self, busy: bool):
        self.busy = busy
        self.capture_btn.configure(state="disabled" if busy else "normal")
        self.status_var.set("AI 正在识别截图…" if busy else "准备就绪 · F8 识别当前屏幕")

    def capture_and_analyze(self):
        if self.busy:
            return
        if ImageGrab is None:
            messagebox.showerror("缺少依赖", f"请先安装 Pillow：{PIL_IMPORT_ERROR}")
            return
        try:
            self.settings = self.settings or load_settings()
        except Exception as exc:
            messagebox.showerror("配置读取失败", str(exc))
            return
        self.set_busy(True)
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        try:
            image = ImageGrab.grab(all_screens=True)
            result = ask_model(self.settings, image)
            self.root.after(0, lambda: self.apply_result(result))
        except Exception as exc:
            self.root.after(0, lambda: messagebox.showerror("识别失败", str(exc)))
        finally:
            self.root.after(0, lambda: self.set_busy(False))

    def apply_result(self, result: dict):
        self.detected_var.set(f"{result.get('detected_text', '未识别到文字')}  ·  {result.get('detected_language', '')}")
        self.meaning_var.set(result.get("meaning_zh", "未能生成中文含义"))
        self.replies = result.get("replies", [])[:3]
        for i, button in enumerate(self.reply_buttons):
            if i < len(self.replies):
                item = self.replies[i]
                text = str(item.get("text", "")).strip()
                translation = str(item.get("translation_zh", "")).strip()
                button.configure(text=f"{i + 1}. {text}（{translation}）", state="normal")
            else:
                button.configure(text=f"{i + 1}. 未生成", state="disabled")

    def copy_selected(self):
        if self.replies:
            self.choose_reply(0)

    def choose_reply(self, index: int):
        if index >= len(self.replies):
            return
        text = clean_reply(str(self.replies[index].get("text", "")))
        if not text:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()
        self.status_var.set(f"已复制：{text} · 请在 VRChat 聊天框按 Ctrl+V")
        # Do not steal focus or inject Enter. This is safer for VRChat and desktop use.


def main():
    root = tk.Tk()
    try:
        root.iconname("VRChat AI 回复助手")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
