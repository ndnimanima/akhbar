"""Creator migration, password protection, feedback delivery and catalog regressions."""
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from app import config, db, security
from app.main import app, _creator_attempts, _creator_unlocks


class CreatorTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:', check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)
        self.p = patch.object(db, '_CONN', self.conn); self.p.start()
        db._seed()
        self.sched = patch.object(config, 'SCHEDULER_ENABLED', False); self.sched.start()
        self.client = TestClient(app); self.client.__enter__()
        self.assertEqual(self.client.post('/api/login', json={'password': config.BOOTSTRAP_PASSWORD}).status_code, 200)

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.sched.stop(); self.p.stop(); self.conn.close(); _creator_attempts.clear(); _creator_unlocks.clear()

    def unlock(self):
        result = self.client.post('/api/creators/unlock', json={'password': config.BOOTSTRAP_PASSWORD})
        self.assertEqual(result.status_code, 200)
        return result.json()['edit_token']

    def test_three_profiles_password_and_secret(self):
        profiles = self.client.get('/api/creators').json()['creators']
        self.assertEqual(len(profiles), 3)
        self.assertEqual(profiles[2]['github'], 'ostovar04')
        self.assertEqual(self.client.patch('/api/creators', json={'edit_token': 'fake', 'creator1_name': 'Oops'}).status_code, 403)
        self.assertEqual(self.client.post('/api/creators/unlock', json={'password': 'wrong'}).status_code, 400)
        self.assertNotEqual(db.get_setting('creator1_name'), 'Oops')
        grant = self.unlock()
        token = '123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcd'
        r = self.client.patch('/api/creators', json={'edit_token': grant, 'creator1_name': 'My name', 'feedback_bot_token': token, 'feedback_chat_id': '123'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(db.get_setting('creator1_name'), 'My name')
        self.assertEqual(self.client.patch('/api/creators', json={'edit_token': grant, 'creator1_name': 'replay'}).status_code, 403)
        self.assertNotIn(token, self.client.get('/api/creators').text)
        self.assertNotIn(token, self.client.get('/api/settings').text)
        self.assertNotIn(token, self.client.get('/api/admin/export').text)
        self.assertEqual(self.client.patch('/api/creators', json={'edit_token': self.unlock(), 'feedback_bot_token': ''}).status_code, 200)
        self.assertEqual(db.get_setting('feedback_bot_token'), token)
        self.client.patch('/api/creators', json={'edit_token': self.unlock(), 'clear_feedback_bot_token': True})
        self.assertEqual(db.get_setting('feedback_bot_token'), '')

    def test_grant_expiration_and_cross_session(self):
        grant = self.unlock()
        with TestClient(app) as other:
            other.post('/api/login', json={'password': config.BOOTSTRAP_PASSWORD})
            self.assertEqual(other.patch('/api/creators', json={'edit_token': grant, 'creator1_name': 'no'}).status_code, 403)
        session = self.client.cookies.get(config.COOKIE_NAME)
        _creator_unlocks[session] = (grant, 0)
        self.assertEqual(self.client.patch('/api/creators', json={'edit_token': grant}).status_code, 403)

    def test_import_cannot_bypass_creator_login(self):
        old_name = db.get_setting('creator1_name')
        payload = {'confirm': True, 'payload': {'settings': {'creator1_name': 'intruder', 'feedback_chat_id': 'intruder'}}}
        self.assertEqual(self.client.post('/api/admin/import', json=payload).status_code, 200)
        self.assertEqual(db.get_setting('creator1_name'), old_name)
        self.assertNotEqual(db.get_setting('feedback_chat_id'), 'intruder')

    def test_feedback_actual_response_and_failure(self):
        token = '123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcd'
        db.set_setting('feedback_bot_token', token); db.set_setting('feedback_chat_id', '123')
        requests = []
        def handler(req):
            requests.append(req)
            return httpx.Response(200, json={'ok': True})
        async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch('httpx.AsyncClient', return_value=async_client):
            r = self.client.post('/api/feedback', json={'kind': 'bug', 'message': 'This is a bug'})
        self.assertTrue(r.json()['forwarded'])
        self.assertTrue(str(requests[0].url).endswith('/bot' + token + '/sendMessage'))
        self.assertEqual(json.loads(requests[0].content)['chat_id'], '123')
        self.assertIn('[bug]', json.loads(requests[0].content)['text'])
        async_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(403, json={'ok': False})))
        with patch('httpx.AsyncClient', return_value=async_client):
            self.assertFalse(self.client.post('/api/feedback', json={'message': 'Hello'}).json()['forwarded'])

    def test_migration_preserves_custom_profile(self):
        db.set_setting('creator1_name', 'Existing author')
        db.execute("DELETE FROM settings WHERE key='creator_profiles_seeded_v1'")
        db._seed()
        self.assertEqual(db.get_setting('creator1_name'), 'Existing author')

    def test_rate_limit(self):
        for _ in range(5):
            self.assertEqual(self.client.post('/api/creators/unlock', json={'password': 'wrong'}).status_code, 400)
        self.assertEqual(self.client.post('/api/creators/unlock', json={'password': config.BOOTSTRAP_PASSWORD}).status_code, 429)

    def test_catalog(self):
        catalog = json.loads(Path('static/assets/source-catalog.json').read_text(encoding='utf8'))
        self.assertEqual(len(catalog['categories']), 9)
        self.assertEqual(sum(len(c['sources']) for c in catalog['categories']), 180)
        self.assertTrue(all(s['url'].startswith('https://') for c in catalog['categories'] for s in c['sources']))

if __name__ == '__main__': unittest.main()
