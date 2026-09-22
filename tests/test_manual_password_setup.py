import ast
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from flask import Flask, jsonify
from core import db, twofa_service
from config import email, register


class ManualPasswordTests(unittest.TestCase):
    def setUp(self):
        # Mount the actual route without starting the production app/recovery jobs.
        tree = ast.parse(Path('webui/app.py').read_text())
        route = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'api_account_password_setup')
        app = Flask(__name__)
        namespace = {'app': app, 'db': db, 'jsonify': jsonify}
        exec(compile(ast.Module(body=[route], type_ignores=[]), 'webui/app.py', 'exec'), namespace)
        self.client = app.test_client()

    def test_manual_password_only_even_when_auto_disabled(self):
        with patch.object(db, 'get_account', return_value={'id': 1, 'email': 'test@example.com', 'access_token': 'fake'}), \
             patch.object(email, 'USE_EMAIL_SERVICE', True), \
             patch.object(register, 'REGISTER_PASSWORD', ''), \
             patch.object(register, 'ENABLE_PASSWORD_SETUP', False), \
             patch.object(twofa_service, 'enqueue_account_security_setup', return_value={'accepted': True, 'future': Mock()}) as enqueue:
            response = self.client.post('/api/accounts/1/password-setup')
        self.assertEqual(response.status_code, 202)
        self.assertNotIn('future', response.json)
        self.assertTrue(enqueue.call_args.kwargs['password_enabled'])
        self.assertFalse(enqueue.call_args.kwargs['totp_enabled'])

    def test_missing_and_already_set(self):
        for account, status in [(None, 404), ({'registration_password': 'already-set'}, 400), ({'email': 'test@example.com'}, 400)]:
            with patch.object(db, 'get_account', return_value=account), \
                 patch.object(twofa_service, 'enqueue_account_security_setup') as enqueue:
                self.assertEqual(self.client.post('/api/accounts/1/password-setup').status_code, status)
                enqueue.assert_not_called()

    def test_busy_and_full(self):
        for result, status in [({'accepted': False, 'busy': True}, 409), ({'accepted': False, 'queue_full': True}, 503)]:
            with patch.object(db, 'get_account', return_value={'access_token': 'fake', 'email': 'test@example.com'}), \
                 patch.object(email, 'USE_EMAIL_SERVICE', True), \
                 patch.object(register, 'REGISTER_PASSWORD', ''), \
                 patch.object(twofa_service, 'enqueue_account_security_setup', return_value=result):
                self.assertEqual(self.client.post('/api/accounts/1/password-setup').status_code, status)
