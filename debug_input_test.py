"""Deterministic debug test for SteamVR trigger gating.

No OpenVR input or VRChat window is touched. The fake action source feeds the
same digital states that ``poll_controller_events`` reads from SteamVR Input.
"""

from __future__ import annotations

from types import SimpleNamespace

import steamvr_overlay as overlay


class FakeInput:
    def __init__(self):
        self.states = {name: False for name in overlay.ACTION_INPUTS}

    def updateActionState(self, _active_sets):
        return None

    def getDigitalActionData(self, handle, _device):
        return SimpleNamespace(bState=self.states.get(handle, False))

    def getAnalogActionData(self, handle, _device):
        return SimpleNamespace(y=0.0)


class FakeSystem:
    def pollNextEvent(self, _event):
        return False

    def getTrackedDeviceClass(self, _device_index):
        return 0

    def getControllerRoleForTrackedDeviceIndex(self, _device_index):
        return 0

    def getControllerState(self, _device_index):
        return False, SimpleNamespace()


def make_overlay(fake_input: FakeInput):
    app = object.__new__(overlay.SteamVROverlay)
    app.input_ready = True
    app.vr_input = fake_input
    app.vr_system = FakeSystem()
    app.action_set_handle = 1
    app.action_handles = {name: name for name in overlay.ACTION_INPUTS}
    app.analog_handles = {"right_joystick": "right_joystick"}
    app.bindings = {"translate": "right_trigger", "confirm_reply": "left_trigger", "recognition_enabled": "on"}
    app.recognition_enabled = True
    app.digital_inputs_armed = False
    app.button_press_started = {}
    app.long_press_fired = set()
    app.next_joystick_move_at = 0.0
    app.joystick_selected_until = 0.0
    app.last_input_diag_at = 0.0
    app.input_feedback_hide_at = None
    app.legacy_trigger_states = {"left": False, "right": False}
    app.legacy_trigger_started = {}
    app.legacy_long_press_fired = set()
    app.action_trigger_active = False
    app.action_joystick_active = False
    app.action_digital_event_seen = False
    app.action_joystick_event_seen = False
    app.fallback_controller_roles = {}
    app.recognitions = 0
    app.closes = 0
    app.feedbacks = 0
    def recognize():
        if app.recognition_enabled:
            app.recognitions += 1
    app.recognize_async = recognize
    app.close_recognition = lambda feedback=False: setattr(app, "closes", app.closes + 1)
    app.disable_recognition = lambda: (setattr(app, "closes", app.closes + 1), setattr(app, "recognition_enabled", False))
    app.show_input_feedback = lambda *_args, **_kwargs: setattr(app, "feedbacks", app.feedbacks + 1)
    app.render_all = lambda: None
    app.show_context_only = lambda: None
    app.move_selection = lambda _delta: None
    return app


class LegacyFakeSystem(FakeSystem):
    def __init__(self):
        self.roles = {
            1: overlay.openvr.TrackedControllerRole_LeftHand,
            2: overlay.openvr.TrackedControllerRole_RightHand,
        }
        self.states = {
            1: SimpleNamespace(ulButtonPressed=0, rAxis=[SimpleNamespace(x=0.0, y=0.0) for _ in range(5)]),
            2: SimpleNamespace(ulButtonPressed=0, rAxis=[SimpleNamespace(x=0.0, y=0.0) for _ in range(5)]),
        }

    def getControllerRoleForTrackedDeviceIndex(self, device_index):
        return self.roles.get(device_index, 0)

    def getTrackedDeviceClass(self, device_index):
        return overlay.openvr.TrackedDeviceClass_Controller if device_index in self.roles else 0

    def getControllerState(self, device_index):
        state = self.states.get(device_index)
        return (state is not None), state


def main() -> int:
    fake = FakeInput()
    app = make_overlay(fake)

    # Startup with both triggers released only arms the input; it must not act.
    app.poll_controller_events()
    assert app.digital_inputs_armed is True
    assert app.recognitions == 0
    assert app.closes == 0

    # A normal right-trigger press/release produces exactly one recognition.
    fake.states["right_trigger"] = True
    app.poll_controller_events()
    fake.states["right_trigger"] = False
    app.poll_controller_events()
    assert app.recognitions == 1
    assert app.closes == 0

    # A long right-trigger hold closes the HUD and never starts recognition.
    fake.states["right_trigger"] = True
    app.poll_controller_events()
    app.button_press_started["translate:right_trigger"] -= overlay.LONG_PRESS_SECONDS + 0.1
    app.poll_controller_events()
    fake.states["right_trigger"] = False
    app.poll_controller_events()
    assert app.recognitions == 1
    assert app.closes == 1
    assert app.recognition_enabled is False
    feedback_before_disabled_press = app.feedbacks

    # A later short press must not reach the model while the master switch is off.
    fake.states["right_trigger"] = True
    app.poll_controller_events()
    fake.states["right_trigger"] = False
    app.poll_controller_events()
    assert app.recognitions == 1
    assert app.feedbacks == feedback_before_disabled_press

    # The keyboard fallback is a no-op while SteamVR Input is available.
    app.hotkeys_registered = False
    app.keyboard_state = {0x77: False}
    app.poll_keyboard_fallback()
    assert app.recognitions == 1

    # Repeat the same checks through the deprecated physical controller-state
    # path, which is the fallback used when SteamVR Input has no binding.
    legacy_system = LegacyFakeSystem()
    legacy_input = FakeInput()
    legacy = make_overlay(legacy_input)
    legacy.vr_system = legacy_system
    legacy.joystick_moves = []
    legacy.move_selection = lambda delta: legacy.joystick_moves.append(delta)
    legacy.poll_controller_events()
    trigger_mask = 1 << int(overlay.BUTTON_CODES["trigger"])
    legacy_system.states[2].ulButtonPressed = trigger_mask
    legacy.poll_controller_events()
    legacy_system.states[2].ulButtonPressed = 0
    legacy.poll_controller_events()
    assert legacy.recognitions == 1
    legacy_system.states[2].ulButtonPressed = trigger_mask
    legacy.poll_controller_events()
    legacy.legacy_trigger_started["right"] -= overlay.LONG_PRESS_SECONDS + 0.1
    legacy.poll_controller_events()
    legacy_system.states[2].ulButtonPressed = 0
    legacy.poll_controller_events()
    assert legacy.closes == 1
    legacy_system.states[1].rAxis[0].y = 1.0
    legacy.poll_controller_events()
    assert legacy.joystick_moves == []
    legacy_system.states[1].rAxis[0].y = 0.0
    legacy_system.states[2].rAxis[0].y = 1.0
    legacy.recognition_enabled = True
    legacy.poll_controller_events()
    assert legacy.joystick_moves == [-1]

    print("debug input test: PASS")
    print("startup released: armed without recognition")
    print("right trigger short press: one recognition")
    print("right trigger long press: one close, no extra recognition")
    print("SteamVR Input ready: F8 fallback ignored")
    print("legacy controller fallback: trigger and right joystick detected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
