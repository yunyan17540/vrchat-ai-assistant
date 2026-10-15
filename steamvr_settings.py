from __future__ import annotations

import json
import tkinter as tk
from pathlib import Path
from tkinter import messagebox


ROOT_DIR = Path(__file__).resolve().parent
CONTROL_PATH = ROOT_DIR / "steamvr_controls.json"
DEFAULTS = {
    "translate": "right_trigger",
    "confirm_reply": "left_trigger",
    "hud_visibility": "auto",
    "text_output": "tapi_osc",
    "clipboard_capture": "off",
    "recognition_enabled": "on",
}
CHOICES = [
    ("左手握把", "left_grip"),
    ("右手握把", "right_grip"),
    ("左手扳机", "left_trigger"),
    ("右手扳机", "right_trigger"),
    ("任一手扳机", "trigger"),
    ("左手 A", "left_a"),
    ("右手 A", "right_a"),
    ("左手 B", "left_b"),
    ("右手 B", "right_b"),
    ("左手触控板", "left_touchpad"),
    ("右手触控板", "right_touchpad"),
    ("左手菜单键", "left_menu"),
    ("右手菜单键", "right_menu"),
]
CONFIRM_CHOICES = [
    ("左手扳机", "left_trigger"),
    ("左手握把", "left_grip"),
    ("左手 A", "left_a"),
    ("左手 B", "left_b"),
    ("左手触控板", "left_touchpad"),
    ("左手菜单键", "left_menu"),
]
LABELS = {
    "translate": "一键识别当前画面",
    "confirm_reply": "选择回复",
    "hud_visibility": "HUD显示方式",
    "text_output": "回复发送方式",
    "clipboard_capture": "剪贴板识别",
    "recognition_enabled": "识别功能总开关",
}
OUTPUT_CHOICES = [
    ("键盘粘贴并回车", "keyboard"),
    ("Tapi / VRChat OSC", "tapi_osc"),
]
HUD_CHOICES = [
    ("识别后显示", "auto"),
    ("始终隐藏", "always_hidden"),
]
CLIPBOARD_CHOICES = [
    ("关闭", "off"),
    ("开启", "on"),
]
RECOGNITION_CHOICES = [
    ("开启（右扳机可识别）", "on"),
    ("关闭（长按后锁定）", "off"),
]
CHOICES_BY_KEY = {
    "translate": CHOICES,
    "confirm_reply": CONFIRM_CHOICES,
    "hud_visibility": HUD_CHOICES,
    "text_output": OUTPUT_CHOICES,
    "clipboard_capture": CLIPBOARD_CHOICES,
    "recognition_enabled": RECOGNITION_CHOICES,
}


def read_values():
    values = dict(DEFAULTS)
    try:
        if CONTROL_PATH.exists():
            data = json.loads(CONTROL_PATH.read_text(encoding="utf-8"))
            for key in values:
                if isinstance(data.get(key), str):
                    values[key] = data[key]
    except Exception:
        pass
    return values


class SettingsWindow:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("VRChat AI 回复助手 · SteamVR 设置")
        self.root.geometry("1080x1060")
        self.root.minsize(920, 900)
        self.root.configure(bg="#f6f8f7")
        self.root.resizable(True, True)
        self.root.option_add("*Font", "{Microsoft YaHei UI} 16")
        self.values = read_values()
        self.vars: dict[str, tk.StringVar] = {}
        self.choice_by_label = {key: {label: value for label, value in choices} for key, choices in CHOICES_BY_KEY.items()}
        self.label_by_choice = {key: {value: label for label, value in choices} for key, choices in CHOICES_BY_KEY.items()}
        self.build()

    def build(self):
        font = ("Microsoft YaHei UI", 17)
        title_font = ("Microsoft YaHei UI", 30, "bold")
        tk.Label(self.root, text="VRChat AI 回复助手", bg="#f6f8f7", fg="#111315", font=title_font).pack(anchor="w", padx=28, pady=(24, 2))
        tk.Label(self.root, text="SteamVR 桌面设置 · 点击整行切换，保存后重启助手读取新按键", bg="#f6f8f7", fg="#4e5758", font=("Microsoft YaHei UI", 13)).pack(anchor="w", padx=34, pady=(0, 28))

        card = tk.Frame(self.root, bg="#ffffff", highlightbackground="#111315", highlightthickness=1)
        card.pack(fill="x", padx=28, pady=4)
        for key, label in LABELS.items():
            row = tk.Frame(card, bg="#ffffff", height=112, cursor="hand2")
            row.pack(fill="x", padx=18, pady=10)
            row.pack_propagate(False)
            tk.Label(row, text=label, bg="#ffffff", fg="#111315", font=font, anchor="w").pack(side="left", padx=(16, 20))
            value = self.label_by_choice[key].get(self.values.get(key), CHOICES_BY_KEY[key][0][0])
            button = tk.Button(
                row,
                text=value,
                command=lambda action=key: self.cycle(action),
                font=("Microsoft YaHei UI", 17, "bold"),
                bg="#ffffff",
                fg="#111315",
                activebackground="#f1e8ed",
                activeforeground="#111315",
                relief="solid",
                bd=2,
                padx=26,
                pady=17,
                width=22,
                cursor="hand2",
            )
            button.pack(side="right", padx=18)
            row.bind("<Button-1>", lambda _event, action=key: self.cycle(action))
            row.winfo_children()[0].bind("<Button-1>", lambda _event, action=key: self.cycle(action))
            self.vars[key] = button

        help_text = (
            "快速教程：\n"
            "1. 右扳机短按：开始识别；识别中会先显示状态卡。\n"
            "2. 右扳机长按约 0.75 秒：关闭 HUD 并锁定识别，需在 SteamVR 设置重新开启。\n"
            "3. 右摇杆上下：选择 1、2、3、4。\n"
            "4. 左扳机：确认当前选项；HUD 卡片不使用光标点击。\n"
            "5. 第 4 项是关闭本次会话。\n"
            "识别成功后才显示回复选项；回复发送方式默认使用 Tapi / VRChat OSC。\n"
            "剪贴板识别开启后优先识别剪贴板文字；关闭时识别 VRChat 画面。"
        )
        tutorial = tk.Frame(self.root, bg="#ffffff", highlightbackground="#111315", highlightthickness=1)
        tutorial.pack(fill="x", padx=28, pady=(28, 18))
        tk.Label(tutorial, text=help_text, justify="left", bg="#ffffff", fg="#4e5758", font=("Microsoft YaHei UI", 14), wraplength=970).pack(anchor="w", padx=22, pady=18)
        buttons = tk.Frame(self.root, bg="#f6f8f7")
        buttons.pack(fill="x", padx=34, pady=12)
        tk.Button(buttons, text="恢复推荐设置", command=self.reset, font=font, bg="#ffffff", fg="#111315", relief="solid", bd=2, padx=24, pady=14).pack(side="left")
        tk.Button(buttons, text="保存设置", command=self.save, font=("Microsoft YaHei UI", 17, "bold"), bg="#111315", fg="#ffffff", activebackground="#313638", relief="solid", bd=2, padx=34, pady=14).pack(side="right")

    def cycle(self, key: str):
        choices = CHOICES_BY_KEY[key]
        current = self.choice_by_label[key].get(self.vars[key]["text"], self.values.get(key, DEFAULTS[key]))
        try:
            index = [value for _, value in choices].index(current)
        except ValueError:
            index = -1
        next_value = choices[(index + 1) % len(choices)]
        self.vars[key].configure(text=next_value[0])

    def reset(self):
        for key, value in DEFAULTS.items():
            self.vars[key].configure(text=self.label_by_choice[key][value])

    def save(self):
        values = {key: self.choice_by_label[key][var["text"]] for key, var in self.vars.items()}
        CONTROL_PATH.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
        messagebox.showinfo("已保存", "设置已保存。下次启动助手时会读取新的按键。")


if __name__ == "__main__":
    root = tk.Tk()
    SettingsWindow(root)
    root.mainloop()
