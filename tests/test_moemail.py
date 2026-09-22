import ast
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from dataclasses import asdict
from flask import Flask, jsonify, request
from core import moemail_client as moe, db, email_provider
from config import email as cfg


class MoeMailTests(unittest.TestCase):
    def setUp(self):
        self.config = patch.multiple(cfg, MOEMAIL_API_BASE='https://mail.example', MOEMAIL_API_KEY='test-key',
                                     MOEMAIL_DOMAIN='mail.example', MOEMAIL_REQUEST_TIMEOUT=20)
        self.config.start()
        self.addCleanup(self.config.stop)
        moe._CONTEXT_CACHE.clear()
        self.addCleanup(moe._CONTEXT_CACHE.clear)

    def test_headers_and_domains(self):
        c = moe.MoeMailClient()
        self.addCleanup(c.close)
        r = Mock(status_code=200)
        r.json.return_value = {'emailDomains': 'mail.example,other.example'}
        with patch.object(c.http, 'request', return_value=r) as req:
            self.assertEqual(c.domains(), ['mail.example', 'other.example'])
        self.assertEqual(req.call_args.kwargs['headers']['X-API-Key'], 'test-key')
        self.assertFalse(req.call_args.kwargs['allow_redirects'])

    def test_pagination_and_repeated_cursor(self):
        c = moe.MoeMailClient()
        self.addCleanup(c.close)
        with patch.object(c, 'request', side_effect=[{'emails': [], 'nextCursor': 'x'},
                {'emails': [{'address': 'target@mail.example', 'id': '2'}], 'nextCursor': None}]):
            self.assertEqual(c.find_mailbox('target@mail.example')['id'], '2')
        with patch.object(c, 'request', return_value={'emails': [], 'nextCursor': 'x'}):
            with self.assertRaises(moe.MoeMailError):
                list(c.pages('/api/emails', 'emails'))

    def test_creation_is_permanent_and_timeout_does_not_retry_post(self):
        c = Mock()
        c.domains.return_value = ['mail.example']
        c.request.side_effect = moe.MoeMailTransportError('timeout')
        c.find_mailbox.return_value = {'id': 'new-id'}
        with patch.object(moe, 'MoeMailClient', return_value=c), patch.object(db, 'save_provider_mailbox') as save:
            first = moe.pick_account()
            second = moe.pick_account()
        self.assertNotEqual(first.email, second.email)
        self.assertEqual(c.request.call_count, 2)
        self.assertEqual(c.request.call_args.kwargs['body']['expiryTime'], 0)
        self.assertEqual(first.email_id, 'new-id')
        self.assertEqual(save.call_count, 4)

    def test_mapping_survives_restart_and_refuses_different_origin(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(db, '_ACCOUNTS_JSON', Path(tmp)/'accounts.json'), \
             patch.object(db, '_SQLITE_READY', False), patch.object(db, '_SQLITE_READY_PATH', None):
            original = moe._store(moe.MoeMailAccount('test@mail.example', 'id', 'https://mail.example'))
            moe._CONTEXT_CACHE.clear()
            self.assertEqual(moe.get_account_context(original.email), original)
            with self.assertRaises(moe.MoeMailError):
                moe.restore_account_context(Mock(base='https://other.example'), original.email)

    def test_otp_filters_old_and_unrelated_messages_preserves_zero(self):
        c = Mock()
        c.pages.return_value = [[
            {'id':'old', 'subject':'ChatGPT code', 'content':'111111', 'received_at':1700000000000},
            {'id':'unrelated', 'subject':'Order 999999', 'content':'shipment', 'received_at':1700000105000},
            {'id':'new', 'subject':'ChatGPT 인증 코드', 'content':'', 'received_at':1700000100000},
        ]]
        c.request.return_value = {'message': {'subject':'ChatGPT', 'content':'인증 코드 012345'}}
        with patch.object(moe, 'MoeMailClient', return_value=c), \
             patch.object(moe, 'restore_account_context', return_value=moe.MoeMailAccount('x@mail.example','id','https://mail.example')):
            self.assertEqual(moe.fetch_latest_otp('x@mail.example', after_ts=1700000090, settle_seconds=0), '012345')
        c.request.assert_called_once_with('GET', '/api/emails/id/new')

    def test_provider_routing(self):
        self.assertEqual(email_provider.parse_email_sources('moemail'), ['moemail'])
        with patch.object(moe, 'pick_account', return_value=Mock(email='new@mail.example')):
            self.assertEqual(email_provider.acquire_email_from_source('moemail'), 'new@mail.example')
        with patch.object(moe, 'fetch_latest_otp', return_value='012345') as fetch:
            self.assertEqual(email_provider.wait_for_otp('x@mail.example', 123, email_source='moemail', force_service=True), '012345')
            self.assertEqual(fetch.call_args.kwargs['after_ts'], 123)

    def test_binding_merges_metadata_without_losing_password_or_totp(self):
        rows = [{'id':1,'email':'old@example.com','extra_json':json.dumps({'registration_password':'saved-password'}), 'totp_secret':'saved-secret'}]
        metadata = asdict(moe.MoeMailAccount('new@mail.example','id','https://mail.example'))
        with patch.object(db, '_load_accounts', return_value=rows), patch.object(db, '_save_accounts'):
            db.finish_account_email_change(1, ok=True, new_email='new@mail.example', source='moemail', email_service=metadata)
        self.assertEqual(db._extract_registration_password(rows[0]), 'saved-password')
        self.assertEqual(rows[0]['totp_secret'], 'saved-secret')
        self.assertEqual(json.loads(rows[0]['extra_json'])['email_service']['email_id'], 'id')

    def test_domains_endpoint_uses_saved_key_and_only_reads(self):
        tree = ast.parse(Path('webui/app.py').read_text())
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name=='api_moemail_domains')
        app = Flask(__name__)
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'webui/app.py','exec'), {'app':app,'request':request,'jsonify':jsonify})
        c = Mock()
        c.domains.return_value = ['mail.example']
        with patch.object(moe, 'MoeMailClient', return_value=c) as client:
            r = app.test_client().post('/api/moemail/domains', json={'api_base':'https://mail.example','api_key':''})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(client.call_args.kwargs['api_key'], 'test-key')
        c.request.assert_not_called()
        c.close.assert_called_once()


if __name__ == '__main__': unittest.main()

class MoeMailIntegrationTests(unittest.TestCase):
    def test_registration_saves_mailbox_metadata(self):
        from core import account_export
        from config import register, twofa
        metadata = asdict(moe.MoeMailAccount('new@mail.example','id','https://mail.example'))
        with patch.object(moe, 'get_account_context_metadata', return_value=metadata), \
             patch.object(db, 'insert_account', return_value=1) as insert, \
             patch.object(register, 'ENABLE_PASSWORD_SETUP', False), patch.object(twofa, 'ENABLE_2FA', False):
            account_export.save_account_data(email='new@mail.example',access_token='fake',email_source='moemail',auto_plan_check=False)
        self.assertEqual(insert.call_args.kwargs['extra']['email_service'], metadata)

    def test_single_and_bulk_fail_config_before_enqueue(self):
        from core import email_change_service
        tree = ast.parse(Path('webui/app.py').read_text())
        names = {'_validate_moemail_source', 'api_account_change_email', 'api_accounts_change_email_bulk'}
        nodes = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in names]
        app = Flask(__name__)
        exec(compile(ast.Module(body=nodes, type_ignores=[]),'webui/app.py','exec'), {'app':app,'db':db,'request':request,'jsonify':jsonify})
        with patch.object(cfg, 'MOEMAIL_API_KEY', ''), patch.object(email_change_service, 'enqueue') as enqueue:
            for path, data in [('/api/accounts/1/change-email',{'source':'moemail'}),
                               ('/api/accounts/change-email-bulk',{'source':'moemail','account_ids':[1,2]})]:
                self.assertEqual(app.test_client().post(path,json=data).status_code,400)
            enqueue.assert_not_called()

    def test_release_does_not_delete_remote_mailbox(self):
        account=moe.MoeMailAccount('x@mail.example','id','https://mail.example')
        with patch.object(moe,'get_account_context',return_value=account), \
             patch.object(db,'save_provider_mailbox') as save, patch.object(moe,'MoeMailClient') as client:
            moe.release_account(account.email)
        save.assert_called_once()
        client.assert_not_called()
