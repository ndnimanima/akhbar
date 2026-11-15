"""Regression tests for article enrichment and single-message publication."""
import asyncio
import hashlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image, ImageDraw
from app import config, db, feeds, pipeline, publish


class DatabaseUpgradeTests(unittest.TestCase):
    def test_upgrade_adds_image_history_without_deleting_existing_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'existing.db'
            conn = sqlite3.connect(path)
            conn.execute('CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)')
            conn.execute('INSERT INTO settings VALUES (?, ?)', ('user_sentinel', 'keep me'))
            conn.commit(); conn.close()
            with patch.object(db, '_CONN', None), patch.object(config, 'DATA_DIR', Path(tmp)), patch.object(config, 'DB_PATH', path):
                conn = db.get_conn()
                self.assertEqual(db.get_setting('user_sentinel'), 'keep me')
                self.assertIsNotNone(db.query_one("SELECT name FROM sqlite_master WHERE type='table' AND name='published_images'"))
                conn.close()


class ArticleTests(unittest.IsolatedAsyncioTestCase):
    def test_listing_never_assigns_listing_photo_and_ignores_sections(self):
        listing = b'''<meta property="og:image" content="https://img.test/banner.jpg">
          <nav><a href="/navigation/story">This navigation link has a sufficiently long title</a></nav>
          <a href="/tag/ai">Artificial intelligence and other very popular topics</a>
          <a href="/story/one">First story with a useful and sufficiently long headline</a>
          <a href="/story/two">Second story with another sufficiently long headline</a>'''
        items = feeds.parse_html(listing, 'https://news.test/tag/ai')
        self.assertEqual([i['link'] for i in items], ['https://news.test/story/one', 'https://news.test/story/two'])
        self.assertEqual([i['image'] for i in items], ['', ''])
        self.assertEqual([i['summary'] for i in items], ['', ''])
        self.assertEqual([i['published'] for i in items], ['', ''])

    def test_article_extracts_its_own_paragraphs_and_image(self):
        text = '''<meta name='description' content='Short description'>
          <meta property='og:image' content='/images/article.jpg'>
          <nav><p>Navigation text that is unrelated and long enough to look like content, so it must be ignored.</p></nav>
          <article><p>The company announced an important change today, affecting many customers and their access to services.</p>
          <p>Officials said the system would be restored gradually after the lengthy interruption.</p>
          <aside><p>This unrelated promotional text must never appear in the extracted article summary.</p></aside></article>'''
        result = feeds.parse_article(text, 'https://news.test/story/one')
        self.assertEqual(result['image'], 'https://news.test/images/article.jpg')
        self.assertIn('important change', result['summary'])
        self.assertIn('restored gradually', result['summary'])
        self.assertNotIn('unrelated', result['summary'])

    def test_falls_back_to_description_not_unrelated_paragraph(self):
        result = feeds.parse_article('<meta name="description" content="An accurate short description."><p>Outside article</p>', 'https://news.test/a')
        self.assertEqual(result['summary'], 'An accurate short description.')

    async def test_article_read_is_bounded(self):
        reads = []
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'<meta property="og:description" content="A useful description here">'
                for i in range(100):
                    reads.append(i)
                    yield b'x' * 16384
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, headers={'content-type':'text/html'}, stream=Stream())))
        with patch.object(feeds.httpx, 'AsyncClient', return_value=client):
            result = await feeds.article_details('https://news.test/story')
        self.assertEqual(result['summary'], 'A useful description here')
        self.assertLess(len(reads), 100)


class PublicationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:', check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)
        self.patcher = patch.object(db, '_CONN', self.conn)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.conn.close()

    async def test_pipeline_enriches_and_sends_once_per_destination_with_dedup(self):
        items = [
            {'title': 'Story one', 'link': 'https://news.test/a', 'summary': '', 'image': '', 'published': ''},
            {'title': 'Story two', 'link': 'https://news.test/b', 'summary': '', 'image': '', 'published': ''},
        ]
        excerpt = 'A genuinely informative article paragraph with enough detail about the event and its actual consequences.'
        async def details(url):
            return {'summary': excerpt, 'image': 'https://img.test/' + url[-1] + '.jpg'}
        image = (b'identical image bytes', 'image.jpg', 'image/jpeg')
        destinations = [{'id': 1, 'platform':'telegram', 'name':'A'}, {'id': 2, 'platform':'telegram', 'name':'B'}]
        job = {'id':1, 'name':'test', 'translate':0, 'include_summary':1, 'send_image':1, 'max_items':8}
        photo, message = AsyncMock(return_value=(True, 'ok')), AsyncMock(return_value=(True, 'ok'))
        with patch.object(pipeline, '_load_feeds', return_value=[{'id':1,'title':'source','url':'https://news.test/'}]), \
             patch.object(pipeline, '_load_destinations', return_value=destinations), \
             patch.object(pipeline, '_load_topics', return_value=[]), \
             patch.object(feeds, 'fetch_source', new=AsyncMock(return_value=items)), \
             patch.object(feeds, 'article_details', side_effect=details), \
             patch.object(feeds, 'within_window', return_value=True), \
             patch.object(publish, 'fetch_image', new=AsyncMock(return_value=image)), \
             patch.object(publish, 'send_photo', photo), patch.object(publish, 'send_message', message), \
             patch.object(pipeline, '_finish_job'), patch.object(pipeline, 'config_send_delay', return_value=0):
            result = await pipeline.run_job(job, force=True)
        self.assertEqual(result['sent'], 4)
        self.assertEqual(result['images'], 2)
        self.assertEqual(result['repeated_images'], 2)
        self.assertEqual(photo.await_count, 2)
        self.assertEqual(message.await_count, 2)
        for call in photo.await_args_list:
            self.assertIn(excerpt, call.args[2])
            self.assertRegex(call.args[2], r'https://news\.test/[ab]')
        for call in message.await_args_list:
            self.assertIn(excerpt, call.args[1])
        self.assertEqual(db.query_one('SELECT count(*) AS c FROM published_images')['c'], 2)
        self.assertTrue(pipeline._image_seen(1, 'https://img.test/a.jpg', hashlib.sha256(b'other').hexdigest()))
        self.assertTrue(pipeline._image_seen(1, 'https://img.test/another.jpg', hashlib.sha256(image[0]).hexdigest()))
        self.assertFalse(pipeline._image_seen(3, 'https://img.test/a.jpg', hashlib.sha256(image[0]).hexdigest()))
        saved = db.query('SELECT * FROM items ORDER BY id')
        self.assertEqual([r['summary'] for r in saved], [excerpt, excerpt])

    async def test_html_source_full_pipeline_uses_article_not_listing(self):
        source = '''<html><head><meta property="og:image" content="https://news.test/banner.jpg"></head>
            <body><a href="/news/article">A useful headline about an important news event</a></body></html>'''
        article = '''<meta property="og:image" content="/own.jpg"><article>
            <p>The complete and specific news explanation tells readers what happened and includes important details about the incident.</p>
            </article>'''
        requests = []
        def respond(request):
            requests.append(str(request.url))
            body = article if request.url.path == '/news/article' else source
            return httpx.Response(200, text=body, headers={'content-type':'text/html'})
        original_client = httpx.AsyncClient
        def make_client(*args, **kwargs):
            return original_client(transport=httpx.MockTransport(respond), **kwargs)
        job = {'id':2, 'name':'test', 'translate':0, 'include_summary':1, 'send_image':1}
        with patch.object(pipeline, '_load_feeds', return_value=[{'id':1,'title':'source','url':'https://news.test/list'}]), \
             patch.object(pipeline, '_load_destinations', return_value=[{'id':1,'platform':'telegram'}]), \
             patch.object(pipeline, '_load_topics', return_value=[]), \
             patch.object(feeds.httpx, 'AsyncClient', side_effect=make_client):
            report = await pipeline.run_job(job, force=True, dry_run=True)
        preview = report['previews'][0]
        self.assertIn('specific news explanation', preview['message'])
        self.assertEqual(preview['image'], 'https://news.test/own.jpg')
        self.assertIn('https://news.test/news/article', requests)
        self.assertFalse(db.query_one('SELECT 1 FROM published_images'))

    def test_historical_url_dedup_is_per_destination(self):
        db.execute('INSERT INTO items(hash,title,image,status,destinations,created_at) VALUES(?,?,?,?,?,?)',
                   ('old','Old','https://img.test/shared.jpg','sent', db.dump([{'id':1,'ok':True}, {'id':2,'ok':False}]), db.now_iso()))
        self.assertTrue(pipeline._image_seen(1, 'https://img.test/shared.jpg', 'other'))
        self.assertFalse(pipeline._image_seen(2, 'https://img.test/shared.jpg', 'other'))

    async def test_ambiguous_photo_failure_does_not_send_another_message(self):
        item = {'title':'Test', 'summary':'An explanation', 'image':'https://img.test/a', 'link':'https://news.test/a'}
        dest = {'id':1, 'platform':'telegram'}
        with patch.object(pipeline, '_image_seen', return_value=False), \
             patch.object(publish, 'send_photo', new=AsyncMock(return_value=(False, 'network_error:timeout'))), \
             patch.object(publish, 'send_message', new=AsyncMock()) as text:
            result = await pipeline._deliver_item(dest, item, (b'abc','a.jpg','image/jpeg'), 'sha')
            self.assertFalse(result[0]); text.assert_not_awaited()
        with patch.object(pipeline, '_image_seen', return_value=False), \
             patch.object(publish, 'send_photo', new=AsyncMock(return_value=(False, 'http_400:bad'))), \
             patch.object(publish, 'send_message', new=AsyncMock(return_value=(True,'ok'))) as text:
            result = await pipeline._deliver_item(dest, item, (b'abc','a.jpg','image/jpeg'), 'sha')
            self.assertTrue(result[0]); self.assertEqual(text.await_count, 1)

    async def test_photo_send_does_not_retry_url_after_timeout(self):
        with patch.object(publish, '_throttle', new=AsyncMock()), \
             patch.object(publish, '_send_photo_telegram', new=AsyncMock(return_value=(False,'network_error:timeout'))), \
             patch.object(publish, '_client') as client:
            result = await publish.send_photo({'platform':'telegram'}, 'https://example.test/a.jpg', 'caption', image=(b'data','a.jpg','image/jpeg'))
        self.assertFalse(result[0]); client.assert_not_called()

    def test_caption_preserves_summary_and_article_link_within_one_message(self):
        item = {'title':'عنوان خبر', 'summary':'توضیح کامل خبر و جزئیات مهم آن.', 'link':'https://example.test/article', 'feed_title':'منبع'}
        text = publish.render_caption(item, {'platform':'telegram', 'parse_mode':'HTML'}, False)
        self.assertIn(item['summary'], text)
        self.assertIn(item['link'], text)
        self.assertNotIn('<a ', text)
        long = {**item, 'title':'🔥' * 200, 'summary':'خبر ' * 200, 'link':'https://example.test/' + 'x' * 2000}
        text = publish.render_caption(long, {'platform':'telegram'}, False)
        self.assertLessEqual(publish._utf16_len(text), 1024)
        self.assertIn('خبر', text)
        self.assertNotIn('https://example.test/', text)  # do not cut URLs
        self.assertNotIn('توضیح کامل', publish.render_caption({**item, '_include_summary':False}, {'platform':'telegram'}, False))
        self.assertNotIn('توضیح کامل', publish.render_item({**item, '_include_summary':False}, {'platform':'telegram'}, False))

    def test_visual_fingerprint_recognises_similar_image_not_unrelated(self):
        img = Image.new('RGB', (220, 120), (210, 50, 30))
        draw = ImageDraw.Draw(img)
        draw.rectangle((20,20,90,90), fill=(20, 80, 220))
        draw.ellipse((120,10,200,100), fill=(50,200,100))
        def encode(image, quality):
            out = io.BytesIO(); image.save(out, format='JPEG', quality=quality); return out.getvalue()
        first, second = encode(img, 92), encode(img.resize((440,240)), 80)
        a_hash, a_color = pipeline._visual_fingerprint(first)
        b_hash, b_color = pipeline._visual_fingerprint(second)
        self.assertTrue(a_hash and b_hash)
        pipeline._remember_image(1, 'https://img.test/first', hashlib.sha256(first).hexdigest(), a_hash, a_color)
        self.assertTrue(pipeline._image_seen(1, 'https://img.test/second', hashlib.sha256(second).hexdigest(), b_hash, b_color))
        other = Image.new('RGB', (220, 120), (200,200,200))
        self.assertEqual(pipeline._visual_fingerprint(encode(other, 90)), ('',''))


if __name__ == '__main__':
    unittest.main()
