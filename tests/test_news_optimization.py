"""Freshness, failures and request-count regressions for news optimizations."""
import asyncio
import sqlite3
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from app import db, feeds, pipeline, publish, translate

RSS = b'''<rss version="2.0"><channel><title>News</title><item><title>Story one</title><link>https://news.test/one</link></item></channel></rss>'''
RSS2 = RSS.replace(b'Story one', b'Story two')


class FeedOptimizationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        feeds._FEED_CACHE.clear()
        self.original_client = httpx.AsyncClient

    def tearDown(self):
        feeds._FEED_CACHE.clear()
        self.assertFalse(feeds._FEED_IN_FLIGHT)

    def clients(self, handler):
        def make_client(*args, **kwargs):
            return self.original_client(transport=httpx.MockTransport(handler), **kwargs)
        return patch.object(feeds.httpx, 'AsyncClient', side_effect=make_client)

    async def test_etag_revalidates_each_check_and_detects_changes(self):
        requests = []
        state = {'body': RSS, 'etag': '"one"', 'fail': False}
        def response(req):
            requests.append(req)
            if state['fail']:
                return httpx.Response(503)
            if req.headers.get('if-none-match') == state['etag']:
                return httpx.Response(304)
            return httpx.Response(200, content=state['body'], headers={'etag':state['etag']})
        with self.clients(response), patch.object(feeds, 'parse_feed', wraps=feeds.parse_feed) as parse:
            first = await feeds.fetch_source({'url':'https://news.test/feed'})
            first[0]['title'] = 'caller changed its copy'
            second = await feeds.fetch_source({'url':'https://news.test/feed'})
            self.assertEqual(second[0]['title'], 'Story one')
            self.assertEqual(parse.call_count, 1, '304 avoids re-parsing')
            self.assertEqual(requests[-1].headers['if-none-match'], '"one"')
            state.update(body=RSS2, etag='"two"')
            self.assertEqual((await feeds.fetch_source({'url':'https://news.test/feed'}))[0]['title'], 'Story two')
            self.assertEqual(parse.call_count, 2)
            state['fail'] = True
            with self.assertRaises(feeds.FeedError):
                await feeds.fetch_source({'url':'https://news.test/feed'})
            state['fail'] = False
            self.assertEqual((await feeds.fetch_source({'url':'https://news.test/feed'}))[0]['title'], 'Story two')
            self.assertNotIn('if-none-match', requests[-1].headers, 'error must invalidate cache')
        self.assertEqual(len(requests), 5, 'each non-overlapping check reaches the origin')

    async def test_last_modified_nostore_and_redirect_do_not_leak_validators(self):
        requests = []
        def response(req):
            requests.append(req)
            if req.url.path == '/redirected':
                return httpx.Response(200, content=RSS)
            if len(requests) == 1:
                return httpx.Response(200, content=RSS, headers={'last-modified':'Wed, 21 Oct 2015 07:28:00 GMT'})
            if len(requests) == 2:
                return httpx.Response(304)
            if req.url.path == '/feed':
                return httpx.Response(302, headers={'location':'https://other.test/redirected'})
            raise AssertionError('unexpected')
        with self.clients(response):
            for _ in range(3):
                await feeds.fetch_source({'url':'https://news.test/feed'})
        self.assertIn('if-modified-since', requests[1].headers)
        self.assertIn('if-modified-since', requests[2].headers)
        self.assertNotIn('if-modified-since', requests[3].headers)
        self.assertNotIn('if-modified-since', requests[4].headers)
        self.assertNotIn('https://news.test/feed', feeds._FEED_CACHE)
        requests.clear()
        with self.clients(lambda req: (requests.append(req) or httpx.Response(200, content=RSS, headers={'etag':'"secret"','cache-control':'no-store'}))):
            await feeds.fetch_source({'url':'https://news.test/private'})
            await feeds.fetch_source({'url':'https://news.test/private'})
        self.assertNotIn('if-none-match', requests[1].headers)

    async def test_cache_is_bounded_and_unsafe_variants_not_saved(self):
        with self.clients(lambda req: httpx.Response(200, content=RSS, headers={'etag':'"v"'})):
            for i in range(feeds._FEED_CACHE_ENTRIES + 2):
                await feeds.fetch_source({'url':f'https://news.test/{i}'})
        self.assertEqual(len(feeds._FEED_CACHE), feeds._FEED_CACHE_ENTRIES)
        self.assertNotIn('https://news.test/0', feeds._FEED_CACHE)
        for headers in ({'etag':'"v"','vary':'cookie'}, {'etag':'"v"','set-cookie':'id=1'}, {'etag':'"v"','cache-control':'private'}):
            with self.clients(lambda req, h=headers: httpx.Response(200, content=RSS, headers=h)):
                await feeds.fetch_source({'url':'https://news.test/unsafe'})
            self.assertNotIn('https://news.test/unsafe', feeds._FEED_CACHE)

    async def test_overlapping_checks_share_request_cancellation_and_retry(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def response(req):
            calls.append(req)
            entered.set()
            await release.wait()
            return httpx.Response(200, content=RSS)
        with self.clients(response):
            a = asyncio.create_task(feeds.fetch_source({'url':'https://news.test/feed'}))
            await entered.wait()
            b = asyncio.create_task(feeds.fetch_source({'url':'https://news.test/feed'}))
            await asyncio.sleep(0)
            a.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await a
            release.set()
            self.assertEqual((await b)[0]['title'], 'Story one')
            self.assertEqual(len(calls), 1)
            await feeds.fetch_source({'url':'https://news.test/feed'})
            self.assertEqual(len(calls), 2, 'completed result must not be reused')
        async def failing(req):
            raise httpx.ConnectError('offline', request=req)
        with self.clients(failing):
            with self.assertRaises(feeds.FeedError):
                await feeds.fetch_source({'url':'https://news.test/fail'})
        with self.clients(lambda req: httpx.Response(200, content=RSS)):
            self.assertEqual((await feeds.fetch_source({'url':'https://news.test/fail'}))[0]['title'], 'Story one')

    async def test_shared_failure_retries_and_results_are_independent(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0
        async def response(req):
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            if calls == 1:
                return httpx.Response(503)
            return httpx.Response(200, content=RSS)
        with self.clients(response):
            first = asyncio.create_task(feeds.fetch_source({'url':'https://news.test/feed'}))
            await entered.wait()
            second = asyncio.create_task(feeds.fetch_source({'url':'https://news.test/feed'}))
            await asyncio.sleep(0)
            release.set()
            result = await asyncio.gather(first, second, return_exceptions=True)
            self.assertTrue(all(isinstance(error, feeds.FeedError) for error in result))
            self.assertEqual(calls, 1)
            # New checks may retry immediately; successful concurrent callers
            # each receive an independent copy of the data.
            entered.clear(); release.clear()
            first = asyncio.create_task(feeds.fetch_source({'url':'https://news.test/feed'}))
            await entered.wait()
            second = asyncio.create_task(feeds.fetch_source({'url':'https://news.test/feed'}))
            await asyncio.sleep(0); release.set()
            one, two = await asyncio.gather(first, second)
            one[0]['title'] = 'edited'
            self.assertEqual(two[0]['title'], 'Story one')
            self.assertEqual(calls, 2)

    async def test_all_waiters_cancel_stops_fetch(self):
        entered, stopped = asyncio.Event(), asyncio.Event()
        async def response(req):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        with self.clients(response):
            task = asyncio.create_task(feeds.fetch_source({'url':'https://news.test/feed'}))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(stopped.wait(), 1)


class ImageOptimizationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:', check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)
        self.patcher = patch.object(db, '_CONN', self.conn)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.conn.close()

    async def test_run_downloads_only_for_destinations_that_need_photo(self):
        url = 'https://img.test/reused.jpg'
        item = {'title':'New headline', 'link':'https://news.test/new', 'summary':'', 'image':url, 'published':''}
        job = {'id':1,'name':'job','translate':0,'include_summary':0,'send_image':1}
        source = [{'id':1,'title':'source','url':'https://news.test/feed'}]
        destinations = [{'id':1,'platform':'telegram'}, {'id':2,'platform':'telegram'}]
        pipeline._remember_image(1, url, 'previous-hash')
        fetch = AsyncMock(return_value=(b'new image','a.jpg','image/jpeg'))
        photo = AsyncMock(return_value=(True, 'ok'))
        message = AsyncMock(return_value=(True, 'ok'))
        async def run_with(dest):
            with patch.object(pipeline, '_load_feeds', return_value=source), \
                 patch.object(pipeline, '_load_destinations', return_value=dest), \
                 patch.object(pipeline, '_load_topics', return_value=[]), \
                 patch.object(feeds, 'fetch_source', AsyncMock(return_value=[item])), \
                 patch.object(publish, 'fetch_image', fetch), patch.object(publish, 'send_photo', photo), \
                 patch.object(publish, 'send_message', message), patch.object(pipeline, '_finish_job'), \
                 patch.object(pipeline, 'config_send_delay', return_value=0):
                return await pipeline.run_job(job, force=True)
        result = await run_with(destinations)
        self.assertEqual((result['sent'],result['images'],result['repeated_images']), (2,1,1))
        self.assertEqual(fetch.await_count, 1)
        self.assertEqual(message.await_args.args[0]['id'], 1)
        self.assertEqual(photo.await_args.args[0]['id'], 2)
        # Both destinations now know the URL; a later different news item does
        # not fetch it at all, but still publishes text to both.
        item['title'] = 'Another headline'
        item['link'] = 'https://news.test/another'
        result = await run_with(destinations)
        self.assertEqual((result['sent'],result['images'],result['repeated_images']), (2,0,2))
        self.assertEqual(fetch.await_count, 1)
        self.assertEqual(message.await_count, 3)

    async def test_successful_translation_is_reused_and_settings_change_invalidates(self):
        db._seed()
        provider = AsyncMock(return_value='ترجمهٔ صحیح')
        with patch.object(translate, '_run_provider', provider):
            self.assertEqual(await translate.translate('English headline'), 'ترجمهٔ صحیح')
            self.assertEqual(await translate.translate('English headline'), 'ترجمهٔ صحیح')
            self.assertEqual(provider.await_count, 1)
            db.set_setting('translate_glossary', 'New term')
            await translate.translate('English headline')
            self.assertEqual(provider.await_count, 2)

    async def test_resend_known_image_and_webhook_do_not_download(self):
        url='https://img.test/old.jpg'
        db.execute('INSERT INTO items(hash,title,image,status,destinations,created_at) VALUES(?,?,?,?,?,?)',
                   ('h','Headline',url,'sent',db.dump([{'id':1,'ok':True}]),db.now_iso()))
        db.execute("INSERT INTO destinations(id,name,platform,chat_id,created_at) VALUES (1,'tg','telegram','c',?)", (db.now_iso(),))
        db.execute("INSERT INTO destinations(id,name,platform,chat_id,created_at) VALUES (2,'hook','webhook','c',?)", (db.now_iso(),))
        fetch = AsyncMock()
        with patch.object(publish, 'fetch_image', fetch), \
             patch.object(publish, 'send_message', AsyncMock(return_value=(True,'ok'))) as message, \
             patch.object(pipeline, 'config_send_delay', return_value=0):
            result = await pipeline.resend_item(1)
        self.assertTrue(result['ok'])
        self.assertEqual(message.await_count, 2)
        fetch.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
