"""Source-check events, authorization and bounded log retrieval."""
import sqlite3
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from app import config, db, feeds, security
from app.main import app


class FeedCheckTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:', check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)
        self.db_patch = patch.object(db, '_CONN', self.conn)
        self.db_patch.start()
        db._seed()
        self.sched = patch.object(config, 'SCHEDULER_ENABLED', False)
        self.sched.start()
        self.client = TestClient(app)
        self.client.__enter__()
        self.feed_id = db.execute("INSERT INTO feeds(title,url,enabled,created_at) VALUES(?,?,?,?)",
                                  ('Test', 'https://example.test/news?token=private', 0, db.now_iso()))
        self.client.post('/api/login', json={'password': config.BOOTSTRAP_PASSWORD})

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.sched.stop(); self.db_patch.stop(); self.conn.close()

    def test_check_records_success_and_failure_without_url(self):
        items = [{'title': 'a', 'published': '2026-01-01'}]
        with patch.object(feeds, 'fetch_source', AsyncMock(return_value=items)), patch.object(feeds, 'within_window', return_value=True):
            response = self.client.post(f'/api/feeds/{self.feed_id}/check')
        self.assertTrue(response.json()['ok'])
        row = db.query_one('SELECT * FROM feeds WHERE id=?', (self.feed_id,))
        self.assertEqual((row['last_status'], row['last_count']), ('ok', 1))
        with patch.object(feeds, 'fetch_source', AsyncMock(side_effect=feeds.FeedError('failed: secret-url'))):
            response = self.client.post(f'/api/feeds/{self.feed_id}/check')
        self.assertFalse(response.json()['ok'])
        self.assertTrue(db.query_one('SELECT last_status FROM feeds WHERE id=?', (self.feed_id,))['last_status'].startswith('error'))
        result = self.client.get('/api/logs?limit=300').json()['logs']
        self.assertEqual([entry['code'] for entry in result[:2]], ['feed_check_failed', 'feed_check_ok'])
        self.assertNotIn('secret-url', str(result))
        self.assertNotIn('private', str(result))
        self.assertEqual(len(self.client.get('/api/logs?limit=-100').json()['logs']), 1)

    def test_management_events_exclude_payloads_and_failed_auth(self):
        secret = 'do-not-log-this-key'
        result = self.client.patch('/api/settings', json={'theme': 'ocean', 'deepseek_api_key': secret})
        self.assertEqual(result.status_code, 200)
        logs = self.client.get('/api/logs').json()['logs']
        self.assertEqual(logs[0]['code'], 'management_action')
        self.assertIn('PATCH /api/settings', logs[0]['message'])
        self.assertNotIn(secret, str(logs))
        self.client.post('/api/logout')
        before = db.query_one("SELECT COUNT(*) AS n FROM logs WHERE code='management_action'")['n']
        self.assertEqual(self.client.patch('/api/settings', json={'theme': 'slate'}).status_code, 401)
        self.assertEqual(before, db.query_one("SELECT COUNT(*) AS n FROM logs WHERE code='management_action'")['n'])

    def test_logs_are_authenticated_and_themes_accepted(self):
        self.client.post('/api/logout')
        self.assertEqual(self.client.get('/api/logs').status_code, 401)
        self.client.post('/api/login', json={'password': config.BOOTSTRAP_PASSWORD})
        for theme in ('aurora', 'ocean'):
            self.assertEqual(self.client.patch('/api/settings', json={'theme': theme}).status_code, 200)
            self.assertEqual(db.get_setting('theme'), theme)
