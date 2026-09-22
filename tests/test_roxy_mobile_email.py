import unittest
from unittest.mock import Mock, patch

from core import roxy_registration as registration


class MobileEmailTests(unittest.TestCase):
    def test_mobile_otp_uses_setter(self):
        driver, element = Mock(), Mock()
        driver.find_elements.return_value = [element]
        with patch.object(registration, '_visible', return_value=True), \
             patch.object(registration, '_set_element_value') as setter, \
             patch.object(registration, '_human_type_text') as typing:
            registration._type_otp_once(driver, '012345', mobile=True)
        setter.assert_called_once_with(driver, element, '012345')
        typing.assert_not_called()

    def test_stale_otp_relocates_and_retry_is_bounded(self):
        from selenium.common.exceptions import StaleElementReferenceException
        driver = Mock()
        with patch.object(registration, '_is_mobile_input_environment', return_value=True), \
             patch.object(registration, '_type_otp_once', side_effect=[StaleElementReferenceException(), None]) as fill:
            registration._type_otp(driver, '012345')
            self.assertEqual(fill.call_count, 2)
        with patch.object(registration, '_is_mobile_input_environment', return_value=True), \
             patch.object(registration, '_type_otp_once', side_effect=StaleElementReferenceException()) as fill:
            with self.assertRaises(StaleElementReferenceException):
                registration._type_otp(driver, '012345')
            self.assertEqual(fill.call_count, 3)

    def test_split_otp_reacquires_boxes_after_each_change(self):
        driver = Mock()
        generations = []
        for _ in range(7):
            boxes = [Mock() for _ in range(6)]
            for box in boxes:
                box.get_attribute.return_value = 'numeric'
            generations.append(boxes)
        driver.find_elements.side_effect = [[], [], [], []] + generations
        with patch.object(registration, '_visible', return_value=True), \
             patch.object(registration, '_set_element_value') as setter:
            registration._type_otp_once(driver, '012345', mobile=True)
        self.assertEqual(setter.call_count, 6)
        for i, call in enumerate(setter.call_args_list):
            self.assertIs(call.args[1], generations[i + 1][i])
            self.assertEqual(call.args[2], '012345'[i])

    def test_mobile_uses_native_setter_without_keyboard_or_mouse(self):
        driver = Mock()
        driver.execute_script.return_value = True
        element = Mock()
        with patch.object(registration, '_set_element_value') as setter, \
             patch.object(registration, '_human_type_text') as typing:
            registration._fill_email_input(driver, element, 'test@example.com')
        setter.assert_called_once_with(driver, element, 'test@example.com')
        typing.assert_not_called()
        element.send_keys.assert_not_called()

    def test_desktop_preserves_existing_input(self):
        driver = Mock()
        driver.execute_script.return_value = False
        element = Mock()
        with patch.object(registration, '_set_element_value') as setter, \
             patch.object(registration, '_human_type_text') as typing:
            registration._fill_email_input(driver, element, 'test@example.com')
        typing.assert_called_once_with(driver, element, 'test@example.com', clear=True)
        setter.assert_not_called()

    def test_allocated_email_uses_mobile_helper_and_verifies_value(self):
        driver = Mock()
        element = Mock()
        email = 'test@example.com'
        with patch.object(registration, '_wait_for_email_input', return_value=element), \
             patch.object(registration, '_fill_email_input') as fill, \
             patch.object(registration, '_email_input_value_state', return_value={'inputs': [{'value': email}]}), \
             patch.object(registration, '_submit_email_step') as submit, \
             patch.object(registration, '_wait_email_submit_next_state', return_value='otp'), \
             patch.object(registration, 'human_delay'):
            result = registration._submit_email_and_wait_next(driver, None, email_supplier=lambda: email)
        fill.assert_called_once_with(driver, element, email)
        submit.assert_called_once_with(driver, email)
        self.assertEqual(result, 'otp')

    def test_existing_email_uses_same_helper(self):
        driver, element = Mock(), Mock()
        with patch.object(registration, '_wait_for_email_input', return_value=element), \
             patch.object(registration, '_fill_email_input') as fill:
            registration._type_email_address(driver, 'test@example.com')
        fill.assert_called_once_with(driver, element, 'test@example.com')


if __name__ == '__main__':
    unittest.main()
