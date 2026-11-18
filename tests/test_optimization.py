"""Resource savings must not change API, retention, or scheduler behaviour."""
import asyncio
import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from app import config, db, video
from app.main import app


class OptimizationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:', check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)
        self.db_patch = patch.object(db, '_CONN', self.conn)
        self.db_patch.start()
        db._seed()

    def tearDown(self):
        self.db_patch.stop()
        self.conn.close()

    def test_snapshot_matches_existing_apis_auth_and_freshness(self):
        report = {'title': 'clip', 'frames': [{'thumb': 'a' * 100000}]}
        db.execute("INSERT INTO video_analyses (url,title,status,stage,report,created_at) VALUES (?,?,?,?,?,?)",
                   ('https://site/a.mp4', 'clip', 'running', 'frames', json.dumps(report), db.now_iso()))
        with patch.object(config, 'SCHEDULER_ENABLED', False), TestClient(app) as client:
            self.assertEqual(client.get('/api/video/snapshot').status_code, 401)
            client.post('/api/login', json={'password': config.BOOTSTRAP_PASSWORD})
            response = client.get('/api/video/snapshot?limit=30&brief=true')
            snap = response.json()
            self.assertEqual(snap['status'], client.get('/api/video/status').json())
            self.assertEqual(snap['analyses'], client.get('/api/video/analyses?limit=30&brief=true').json()['analyses'])
            self.assertNotIn('report', snap['analyses'][0])
            self.assertLess(len(response.content), 3000)
            db.execute("UPDATE video_analyses SET status='done',stage='done' WHERE id=1")
            full = client.get('/api/video/snapshot?limit=30').json()
            self.assertEqual(full['analyses'][0]['report'], report)
            self.assertEqual(full['status']['done'], 1)
            self.assertEqual(full['status']['total'], 1)
            self.assertEqual(full['analyses'][0]['stage'], 'done')

    def test_settings_and_smart_disabled_use_one_read(self):
        statements = []
        self.conn.set_trace_callback(statements.append)
        cfg = video.settings()
        self.assertFalse(cfg['smart'])
        self.assertEqual(len(statements), 1)
        statements.clear()
        self.assertEqual(asyncio.run(video.maybe_smart_tick()), [])
        self.assertEqual(len(statements), 1)
        self.conn.set_trace_callback(None)

    def test_smart_mode_respects_interval_and_picks_up_due_work(self):
        db.set_setting('video_smart', '1')
        db.set_setting('video_interval_minutes', '30')
        db.set_setting('video_smart_last_run', datetime.now(timezone.utc).isoformat())
        with patch.object(video, 'pending_video_urls', return_value=[{'url':'https://x/a.mp4','title':'A'}]) as pending, \
             patch.object(video, 'start_analysis', return_value=42) as start:
            self.assertEqual(asyncio.run(video.maybe_smart_tick()), [])
            pending.assert_not_called()
            db.set_setting('video_smart_last_run', (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat())
            self.assertEqual(asyncio.run(video.maybe_smart_tick()), [42])
            start.assert_called_once_with('https://x/a.mp4', title_hint='A', mode='auto')
            self.assertEqual(asyncio.run(video.maybe_smart_tick()), [])

    def test_log_retains_latest_3000_despite_id_gaps_and_single_commit(self):
        self.conn.executemany('INSERT INTO logs(id,job_id,level,message,code,created_at) VALUES(?,NULL,?,?,?,?)',
                              [(i * 2, 'info', str(i), '', db.now_iso()) for i in range(1, 3002)])
        self.conn.commit()
        statements = []
        self.conn.set_trace_callback(statements.append)
        db.log(None, 'new')
        self.conn.set_trace_callback(None)
        ids = [r['id'] for r in db.query('SELECT id FROM logs ORDER BY id')]
        self.assertEqual(len(ids), 3000)
        self.assertEqual(ids[0], 6)
        self.assertEqual(ids[-1], 6003)
        self.assertEqual(sum(s.upper() == 'COMMIT' for s in statements), 1)
        self.assertEqual(db.query_one('SELECT message FROM logs ORDER BY id DESC LIMIT 1')['message'], 'new')


if __name__ == '__main__':
    unittest.main()
