import unittest

from core.cloakbrowser_driver import CloakElement


class _Keyboard:
    def __init__(self):
        self.typed = []
        self.pressed = []

    def type(self, text, delay=0):
        self.typed.append((text, delay))

    def press(self, key):
        self.pressed.append(key)


class _Page:
    def __init__(self):
        self.keyboard = _Keyboard()


class _Locator:
    pass


class CloakElementTests(unittest.TestCase):
    def test_set_value_uses_single_dom_evaluation(self):
        page = _Page()
        element = CloakElement(page, locator=_Locator())
        calls = []
        element._eval = lambda expression, arg=None: calls.append((expression, arg)) or arg

        result = element.set_value("nimble@example.com")

        self.assertEqual(result, "nimble@example.com")
        self.assertEqual(calls[0][1], "nimble@example.com")

    def test_send_keys_uses_dom_editor_for_each_text_chunk(self):
        page = _Page()
        locator = _Locator()
        element = CloakElement(page, locator=locator)
        calls = []
        element._edit_value = lambda text, key="insert": calls.append((text, key))

        element.send_keys("ab")
        element.send_keys("cd")

        self.assertEqual(calls, [("ab", "insert"), ("cd", "insert")])

    def test_send_keys_maps_selenium_backspace(self):
        page = _Page()
        element = CloakElement(page, locator=_Locator())
        calls = []
        element._edit_value = lambda text, key="insert": calls.append((text, key))

        element.send_keys("\ue003")

        self.assertEqual(calls, [("", "backspace")])


if __name__ == "__main__":
    unittest.main()
