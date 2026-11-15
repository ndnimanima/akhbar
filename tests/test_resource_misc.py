"""Resource regressions for feeds, publication and video polling."""
import asyncio
import unittest
from unittest.mock import patch

import httpx
from app import feeds, pipeline, video


class ResourceMiscTests(unittest.IsolatedAsyncioTestCase):
    async def test_og_image_stops_after_preview_limit(self):
        reads = []
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'<meta property="og:image" content="https://img.test/p.jpg">'
                for i in range(100):
                    reads.append(i)
                    yield b'x' * 16384
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=Stream())))
        with patch.object(feeds.httpx, 'AsyncClient', return_value=client):
            image = await feeds.og_image('https://news.test/a')
        self.assertEqual(image, 'https://img.test/p.jpg')
        self.assertLess(len(reads), 100)

    async def test_filtered_results_preserve_counts_and_storage_cap(self):
        items = [{'title': f'item {i}', 'link': f'https://news.test/{i}', 'summary': '', 'published': ''} for i in range(100)]
        async def fetch(feed): return items
        job = {'id': 1, 'name': 'test', 'topic_ids': '[1]', 'window_hours': 24}
        with patch.object(pipeline, '_load_feeds', return_value=[{'id': 1, 'url': 'https://news.test', 'title': 'feed'}]), \
             patch.object(pipeline, '_load_destinations', return_value=[{'id': 1}]), \
             patch.object(pipeline, '_load_topics', return_value=[]), \
             patch.object(feeds, 'fetch_source', fetch), \
             patch.object(feeds, 'within_window', return_value=True), \
             patch.object(pipeline, '_seen', return_value=set()), \
             patch.object(pipeline.topics_mod, 'score_topics', return_value=(None, 0, set())), \
             patch.object(pipeline, '_insert_item') as insert, \
             patch.object(pipeline, '_finish_job'), patch.object(pipeline.db, 'execute'), patch.object(pipeline.db, 'log'):
            result = await pipeline.run_job(job, force=True)
            self.assertEqual(result['skipped'], 100)
            self.assertEqual(result['matched'], 0)
            self.assertEqual(insert.call_count, pipeline.MAX_FILTERED_STORED)
            insert.reset_mock()
            result = await pipeline.run_job(job, force=True, dry_run=True)
            self.assertEqual(result['skipped'], 100)
            insert.assert_not_called()

    async def test_brief_video_list_does_not_fetch_report(self):
        with patch.object(video.db, 'query', return_value=[]) as query:
            video.list_jobs(-1, brief=True)
        sql, params = query.call_args.args
        self.assertNotIn('report', sql.lower())
        self.assertEqual(params, (1,))


class VideoListIntegrationTests(unittest.TestCase):
    def test_smart_video_candidates_exclude_previous_and_duplicates(self):
        import sqlite3
        from app import db
        conn = sqlite3.connect(':memory:', check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(db.SCHEMA)
        for i, link in enumerate(['https://site/a.mp4', 'https://site/a.mp4', 'https://site/b.mp4', 'https://site/c.mp4']):
            conn.execute('INSERT INTO items (hash, title, link, created_at) VALUES (?, ?, ?, ?)',
                         (str(i), f'title {i}', link, '2025-01-01'))
        conn.execute('INSERT INTO video_analyses (url, created_at) VALUES (?, ?)',
                     (' https://site/b.mp4 ', '2025-01-01'))
        conn.commit()
        try:
            with patch.object(db, '_CONN', conn):
                rows = video.pending_video_urls(limit=10)
            self.assertEqual([row['url'] for row in rows], ['https://site/c.mp4', 'https://site/a.mp4'])
        finally:
            conn.close()

    def test_brief_and_full_api_return_expected_data(self):
        import json
        import sqlite3
        from fastapi.testclient import TestClient
        from app import config, db, security
        from app.main import app

        conn = sqlite3.connect(':memory:', check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(db.SCHEMA)
        report = {'title': 'clip', 'frames': [{'thumb': 'a' * 100000}], 'transcript': 'hello'}
        conn.execute("INSERT INTO video_analyses (url, title, status, stage, mode, report, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     ('https://test/clip.mp4', 'clip', 'done', 'done', 'manual', json.dumps(report), '2025-01-01'))
        conn.commit()
        app.dependency_overrides[security.require_auth] = lambda: None
        try:
            with patch.object(db, '_CONN', conn), patch.object(config, 'SCHEDULER_ENABLED', False), TestClient(app) as client:
                brief = client.get('/api/video/analyses?brief=true').json()['analyses']
                full = client.get('/api/video/analyses').json()['analyses']
                self.assertNotIn('report', brief[0])
                self.assertEqual(full[0]['report'], report)
                self.assertLess(len(json.dumps(brief)), len(json.dumps(full)) // 100)
                details = client.get('/api/video/jobs/1').json()['job']
                self.assertEqual(details['report'], report)
        finally:
            app.dependency_overrides.pop(security.require_auth, None)
            conn.close()


if __name__ == "__main__":
    unittest.main()
