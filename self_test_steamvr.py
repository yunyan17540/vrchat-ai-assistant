"""Offline smoke test for the SteamVR assistant.

This deliberately avoids the provider API and never touches VRChat input. It
checks the pieces that can be verified without a headset click: settings,
reply sanitising, PNG rendering, and the expected overlay sizes.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import steamvr_overlay as overlay
from vrchat_ai_translator import clean_reply


ROOT = Path(__file__).resolve().parent


def main() -> int:
    controls = overlay.load_bindings()
    allowed = set(overlay.DASHBOARD_CHOICES) | {"trigger"}
    assert set(controls) == set(overlay.BINDING_DEFAULTS) | set(overlay.OUTPUT_DEFAULTS)
    assert all(controls[key] in allowed for key in overlay.BINDING_DEFAULTS)
    assert controls["confirm_reply"] in overlay.CONFIRM_CHOICES
    assert controls["translate"] == "right_trigger"
    assert controls["confirm_reply"] == "left_trigger"
    assert controls["text_output"] in {"keyboard", "tapi_osc"}
    assert controls["clipboard_capture"] in {"off", "on"}
    assert controls["hud_visibility"] in {"auto", "always_hidden"}
    assert controls["recognition_enabled"] in {"on", "off"}
    assert overlay.SteamVROverlay.dashboard_mouse_coordinates(0, 0) == (0.0, 1000.0)
    assert overlay.SteamVROverlay.dashboard_mouse_coordinates(1600, 1000) == (1600.0, 0.0)

    sample = "はーい！待ってるね！（好——！等你哦！）"
    assert clean_reply(sample) == "はーい！待ってるね！"

    state = {
        "bindings": controls,
        "detected": "これは認識テストです。",
        "meaning": "这是识别测试。",
        "replies": [
            {"text": "はーい！待ってるね！", "translation_zh": "好——！等你哦！"},
            {"text": "了解しました。", "translation_zh": "明白了。"},
            {"text": "少々お待ちください。", "translation_zh": "请稍等。"},
        ],
        "hover_reply": None,
        "pressed_reply": None,
    }
    with tempfile.TemporaryDirectory(prefix="vrchat-ai-self-test-") as temp:
        target = Path(temp)
        context = overlay.context_image(state)
        assert context.size == overlay.OVERLAY_SPECS["context"][0]
        context.save(target / "context.png")
        for index in range(3):
            image = overlay.reply_image(index, state)
            assert image.size == overlay.OVERLAY_SPECS[f"reply{index}"][0]
            image.save(target / f"reply{index}.png")
        close = overlay.close_image(state)
        assert close.size == overlay.OVERLAY_SPECS["close"][0]
        close.save(target / "close.png")

    report = {
        "settings": "ok",
        "reply_filter": "ok",
        "rendering": "ok",
        "reply_count": 3,
        "selection_count": overlay.SELECTION_COUNT,
        "next_manual_hook": "在 VR 中用右摇杆选择 3 条回复或第 4 项关闭会话，再用左扳机确认。",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
