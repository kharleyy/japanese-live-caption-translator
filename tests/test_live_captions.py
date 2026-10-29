from __future__ import annotations

import unittest

from realtime_translator.live_captions import (
    is_japanese_language_name,
    prepare_live_captions,
)


class FakeInvokePattern:
    def __init__(self, callback) -> None:
        self.callback = callback

    def Invoke(self) -> bool:
        self.callback()
        return True


class FakeWindow:
    def __init__(self, children) -> None:
        self.children = children
        self.AutomationId = ""
        self.Name = "实时辅助字幕"
        self.ControlTypeName = "WindowControl"

    def GetChildren(self):
        return list(self.children)


class FakeCombo:
    def __init__(self) -> None:
        self.AutomationId = "SpeechModelDropDown"
        self.Name = "实时字幕语言"
        self.ControlTypeName = "ComboBoxControl"
        self.selected = False

    def GetChildren(self):
        return []

    def Select(self, *, condition, simulateMove=True) -> bool:
        self.selected = condition("日语(日本)")
        return self.selected


class FakeButton:
    def __init__(self, callback) -> None:
        self.AutomationId = ""
        self.Name = "继续"
        self.ControlTypeName = "ButtonControl"
        self.callback = callback
        self.invoked = False

    def GetChildren(self):
        return []

    def GetInvokePattern(self):
        return FakeInvokePattern(self._invoke)

    def _invoke(self) -> None:
        self.invoked = True
        self.callback()


class FakeReadyText:
    AutomationId = "ReadyToCaptionTextBlock"
    Name = "已准备好在 日语(日本) 中显示实时字幕"
    ControlTypeName = "TextControl"

    def GetChildren(self):
        return []


class LiveCaptionsLanguageTests(unittest.TestCase):
    def test_matches_observed_japanese_language_names(self) -> None:
        for value in (
            "日语(日本)",
            "日语（日本）",
            "日本語(日本)",
            "Japanese (Japan)",
        ):
            with self.subTest(value=value):
                self.assertTrue(is_japanese_language_name(value))

    def test_rejects_other_languages(self) -> None:
        for value in ("中文(中国)", "English (United States)"):
            with self.subTest(value=value):
                self.assertFalse(is_japanese_language_name(value))

    def test_prepares_japanese_then_activates_continue(self) -> None:
        combo = FakeCombo()
        window = FakeWindow([combo])

        def on_continue() -> None:
            window.children = [FakeReadyText()]

        button = FakeButton(on_continue)
        window.children.append(button)

        state = prepare_live_captions(window, timeout_seconds=2)

        self.assertEqual(state, "ready")
        self.assertTrue(combo.selected)
        self.assertTrue(button.invoked)


if __name__ == "__main__":
    unittest.main()
