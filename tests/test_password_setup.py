import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from contextlib import ExitStack

from core import password_setup as protocol, twofa_service as service, db


class PasswordProtocolTests(unittest.TestCase):
    def test_password_generation_and_validation(self):
        value = protocol.choose_password()
        self.assertEqual(len(value), 16)
        self.assertNotEqual(value, protocol.choose_password())
        with self.assertRaises(protocol.PasswordSetupError):
            protocol.choose_password('short')

    def test_email_otp_without_mfa_reaches_password_page(self):
        session = Mock()
        result = {'continue_url': 'https://auth.openai.com/reset-password/new-password',
                  'page': {'type': 'reset_password_new_password'}}
        with patch.object(protocol, '_navigate', side_effect=lambda s, u: u), \
             patch.object(protocol, '_post_auth', return_value=result) as post, \
             patch('core.email_provider.wait_for_otp', return_value='012345') as otp:
            url = protocol.complete_reauthentication(session, 'test@example.com',
                'https://auth.openai.com/email-verification', after_ts=123)
        self.assertTrue(url.endswith('/new-password'))
        self.assertEqual(post.call_count, 1)
        otp.assert_called_once_with('test@example.com', after_ts=123)

    def test_password_confirmed_before_callback_failure(self):
        callback = {'continue_url': 'https://chatgpt.com/api/auth/callback/openai?code=fake',
                    'page': {'type': 'external_url'}}
        confirmed = Mock()
        with patch.object(protocol, '_trigger', return_value='https://auth.openai.com/email-verification'), \
             patch.object(protocol, 'complete_reauthentication', return_value='https://auth.openai.com/reset-password/new-password'), \
             patch.object(protocol, '_post_auth', return_value=callback), \
             patch.object(protocol, '_navigate', side_effect=RuntimeError('sensitive')):
            result = protocol.setup_password(Mock(), 'test@example.com', 'Long-password-123', on_confirmed=confirmed)
        confirmed.assert_called_once_with('Long-password-123')
        self.assertTrue(result['ok'])
        self.assertIn('session_refresh_error', result)
        self.assertNotIn('sensitive', str(result))

    def test_add_timeout_is_unknown_and_never_retried(self):
        session = Mock()
        session.get_auth_headers.return_value = {}
        session.post.side_effect = TimeoutError('secret-proxy')
        with self.assertRaises(protocol.PasswordResultUnknown):
            protocol._post_auth(session, '/api/accounts/password/add', {'password': 'secret'},
                                referer='https://auth.openai.com/reset-password/new-password', password_submission=True)
        self.assertEqual(session.post.call_count, 1)

    def test_unknown_or_external_state_is_rejected(self):
        with self.assertRaises(protocol.PasswordSetupError):
            protocol._trusted_url('https://evil.example/steal')
        with patch.object(protocol, '_navigate', side_effect=lambda s, u: u):
            with self.assertRaises(protocol.PasswordSetupError):
                protocol.complete_reauthentication(Mock(), 'test@example.com',
                    'https://auth.openai.com/unknown-page', after_ts=123)


class SecurityQueueTests(unittest.TestCase):
    def test_password_failure_still_runs_2fa_and_logs_separately(self):
        order = []
        def password(*args, **kwargs):
            order.append('password')
            raise protocol.PasswordSetupError('password rejected')
        def totp(*args, **kwargs):
            order.append('totp')
            return 'fake-totp-secret'
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            stack.enter_context(patch.object(service, '_LOG_DIR', Path(tmp)))
            stack.enter_context(patch.object(service, '_QUEUE_SLOTS', Mock()))
            stack.enter_context(patch.object(service, 'BrowserSession', return_value=Mock()))
            stack.enter_context(patch.object(service._email_cfg, 'USE_EMAIL_SERVICE', True))
            stack.enter_context(patch.object(protocol, 'setup_password', side_effect=password))
            stack.enter_context(patch.object(service, 'setup_2fa', side_effect=totp))
            stack.enter_context(patch.object(db, 'get_account', return_value={'id': 1}))
            for name in ('mark_account_security_setup', 'mark_account_totp_setup_running',
                         'update_account_password_setup', 'update_account_totp_secret'):
                stack.enter_context(patch.object(db, name, return_value=True))
            result = service._run_twofa(account_id=1, email='test@example.com', access_token='fake',
                                       proxy=None, trigger='test', password_enabled=True, totp_enabled=True)
            self.assertEqual(order, ['password', 'totp'])
            self.assertFalse(result['password']['ok'])
            self.assertTrue(result['totp']['ok'])
            self.assertTrue(service.password_log_path('test@example.com').exists())

    def test_db_merge_and_lock(self):
        rows = [{'id': 1, 'email': 'test@example.com', 'extra_json': json.dumps({'other': 1})}]
        with patch.object(db, '_load_accounts', return_value=rows), patch.object(db, '_save_accounts'):
            self.assertTrue(db.claim_account_security_setup(1, password_enabled=True, totp_enabled=True))
            self.assertFalse(db.claim_account_totp_setup(1))
            self.assertFalse(db.claim_account_security_setup(1, password_enabled=True, totp_enabled=False))
            db.update_account_password_setup(1, {'ok': True, 'status': 'success', 'password': 'Long-password-123'})
            self.assertEqual(json.loads(rows[0]['extra_json'])['other'], 1)
            self.assertEqual(db._extract_registration_password(rows[0]), 'Long-password-123')
            db.recover_interrupted_totp_setups()
            self.assertEqual(rows[0]['password_setup_status'], 'success')
            self.assertEqual(rows[0]['totp_setup_status'], 'failed')

    def test_manual_2fa_does_not_enable_password(self):
        with patch.object(service._email_cfg, 'USE_EMAIL_SERVICE', True), \
             patch.object(service, 'enqueue_account_security_setup', return_value={'accepted': True}) as enqueue:
            service.enqueue_account_totp_setup(account_id=1, email='test@example.com', access_token='fake')
        self.assertFalse(enqueue.call_args.kwargs['password_enabled'])
        self.assertTrue(enqueue.call_args.kwargs['totp_enabled'])


if __name__ == '__main__':
    unittest.main()

class AutoSetupIntegrationTests(unittest.TestCase):
    def test_all_switch_combinations(self):
        from core import account_export
        from config import register, twofa
        for password in (False, True):
            for totp in (False, True):
                with self.subTest(password=password, totp=totp), \
                     patch.object(register, 'ENABLE_PASSWORD_SETUP', password), \
                     patch.object(twofa, 'ENABLE_2FA', totp), \
                     patch.object(db, 'insert_account', return_value=7), \
                     patch.object(service, 'enqueue_account_security_setup', return_value={'accepted': True}) as enqueue:
                    account_export.save_account_data(email='test@example.com', access_token='fake', auto_plan_check=False)
                    if password or totp:
                        self.assertEqual(enqueue.call_args.kwargs['password_enabled'], password)
                        self.assertEqual(enqueue.call_args.kwargs['totp_enabled'], totp)
                    else:
                        enqueue.assert_not_called()

    def test_config_rejects_short_password_without_writing(self):
        from webui import config_editor
        with patch('config.env_loader.write_env_values') as write:
            with self.assertRaises(ValueError):
                config_editor.update_config({'REGISTER_PASSWORD': 'short'})
            write.assert_not_called()

    def test_mfa_branch_uses_stored_secret_only_when_required(self):
        results = [
            {'continue_url': 'https://auth.openai.com/mfa-challenge/fake', 'page': {'type': 'mfa_challenge',
             'payload': {'factors': [{'id': 'fake', 'factor_type': 'totp'}]}}},
            {},
            {'continue_url': 'https://auth.openai.com/reset-password/new-password', 'page': {'type': 'reset_password_new_password'}},
        ]
        with patch.object(protocol, '_navigate', side_effect=lambda s,u:u), \
             patch.object(protocol, '_post_auth', side_effect=results) as post, \
             patch('core.email_provider.wait_for_otp', return_value='012345'):
            protocol.complete_reauthentication(Mock(), 'test@example.com',
                'https://auth.openai.com/email-verification', after_ts=1, totp_secret='JBSWY3DPEHPK3PXP')
        self.assertEqual([c.args[1] for c in post.call_args_list], [
            '/api/accounts/email-otp/validate', '/api/accounts/mfa/issue_challenge', '/api/accounts/mfa/verify'])

class PasswordCsrfDiagnosticsTests(unittest.TestCase):
    def test_providers_precedes_csrf_and_signin(self):
        session = Mock()
        session.device_id = 'fake-device'
        session.get_nextauth_headers.return_value = {}
        def response(payload):
            r = Mock(status_code=200)
            r.json.return_value = payload
            return r
        session.get.side_effect = [response({'openai': {}}), response({'csrfToken': 'fake-csrf'})]
        session.post.return_value = response({'url': 'https://auth.openai.com/api/accounts/authorize'})
        protocol._trigger(session, 'test@example.com')
        self.assertEqual([c.args[0] for c in session.get.call_args_list], [
            'https://chatgpt.com/api/auth/providers', 'https://chatgpt.com/api/auth/csrf'])
        self.assertEqual(session.post.call_count, 1)

    def test_challenge_stops_before_signin_without_logging_body(self):
        session = Mock()
        session.get_nextauth_headers.return_value = {}
        r = Mock(status_code=403, headers={'content-type': 'text/html', 'cf-mitigated': 'challenge'},
                 text='private-cookie-value just a moment')
        r.json.side_effect = ValueError()
        session.get.return_value = r
        with self.assertLogs('core.password_setup', level='INFO') as logs:
            with self.assertRaisesRegex(protocol.PasswordSetupError, '访问挑战'):
                protocol._trigger(session, 'test@example.com')
        session.post.assert_not_called()
        self.assertEqual(session.get.call_count, 1)
        self.assertNotIn('private-cookie-value', str(logs.output))
