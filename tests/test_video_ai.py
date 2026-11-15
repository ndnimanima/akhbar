"""Video AI settings, evidence coverage and API security regressions."""
import json
import sqlite3
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient
from app import config, db, security, video
from app.main import app


class VideoAIAPITests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:', check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)
        self.db_patch = patch.object(db, '_CONN', self.conn)
        self.db_patch.start()
        app.dependency_overrides[security.require_auth] = lambda: None
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        app.dependency_overrides.pop(security.require_auth, None)
        self.db_patch.stop()
        self.conn.close()

    def test_settings_keep_clear_and_never_export_secret(self):
        payload = {'base_url': 'https://api.example.test/v1', 'model': 'vision-model', 'api_key': 'SECRET-KEY', 'vision': True}
        r = self.client.put('/api/video/ai-settings', json=payload)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()['settings']['ai_key_set'])
        self.assertNotIn('SECRET-KEY', r.text)
        self.assertNotIn('SECRET-KEY', self.client.get('/api/video/status').text)
        self.assertNotIn('SECRET-KEY', self.client.get('/api/settings').text)
        self.assertNotIn('SECRET-KEY', self.client.get('/api/admin/export').text)
        payload['api_key'] = ''
        self.client.put('/api/video/ai-settings', json=payload)
        self.assertEqual(db.get_setting('video_ai_api_key'), 'SECRET-KEY')
        self.assertEqual(self.client.put('/api/video/ai-settings', json={**payload, 'base_url': 'http://bad.test'}).status_code, 400)
        self.assertEqual(db.get_setting('video_ai_base_url'), 'https://api.example.test/v1')
        self.assertEqual(db.get_setting('video_ai_api_key'), 'SECRET-KEY')
        self.assertTrue(self.client.patch('/api/video/settings', json={'smart': True}).json()['settings']['smart'])
        payload['clear_key'] = True
        self.client.put('/api/video/ai-settings', json=payload)
        self.assertFalse(video.settings()['ai_key_set'])
        self.assertTrue(video.settings()['smart'])

    def test_switch_provider_requires_new_key_and_preserves_old_settings(self):
        old = {'provider': 'openai_compatible', 'base_url': 'https://api.example.test/v1', 'model': 'original', 'api_key': 'old-secret'}
        self.assertEqual(self.client.put('/api/video/ai-settings', json=old).status_code, 200)
        new = {'provider': 'gemini', 'base_url': 'https://generativelanguage.googleapis.com/v1beta', 'model': 'gemini-2.5-flash'}
        rejected = self.client.put('/api/video/ai-settings', json=new)
        self.assertEqual(rejected.json()['detail'], 'video_ai_new_key_required')
        self.assertEqual(video.settings()['ai_provider'], 'openai_compatible')
        self.assertEqual(db.get_setting('video_ai_api_key'), 'old-secret')
        rejected = self.client.put('/api/video/ai-settings', json={**old, 'base_url': 'https://other.example.test/v1', 'api_key': ''})
        self.assertEqual(rejected.json()['detail'], 'video_ai_new_key_required')
        self.assertEqual(self.client.put('/api/video/ai-settings', json={**new, 'api_key': 'new-secret'}).status_code, 200)
        self.assertEqual(video.settings()['ai_provider'], 'gemini')
        self.assertEqual(db.get_setting('video_ai_api_key'), 'new-secret')
        invalid = self.client.put('/api/video/ai-settings', json={**new, 'provider': 'unknown'})
        self.assertEqual(invalid.json()['detail'], 'video_ai_provider_invalid')
        self.assertEqual(video.settings()['ai_provider'], 'gemini')

    def test_backup_import_cannot_redirect_existing_secret(self):
        payload = {'provider': 'gemini', 'base_url': 'https://generativelanguage.googleapis.com/v1beta', 'model': 'test', 'api_key': 'my-secret'}
        self.assertEqual(self.client.put('/api/video/ai-settings', json=payload).status_code, 200)
        backup = {'settings': {'video_ai_base_url': 'https://attacker.test/v1', 'video_ai_provider': 'anthropic', 'video_ai_model': 'new-model', 'video_ai_api_key': 'bad'}}
        r = self.client.post('/api/admin/import', json={'confirm': True, 'payload': backup})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(video.settings()['ai_base_url'], payload['base_url'])
        self.assertEqual(video.settings()['ai_provider'], 'gemini')
        self.assertEqual(db.get_setting('video_ai_api_key'), 'my-secret')
        self.assertEqual(video.settings()['ai_model'], 'new-model')

    def test_analyze_is_manual_and_does_not_enable_smart(self):
        self.assertFalse(video.settings()['smart'])
        with patch.object(video, 'start_analysis', return_value=42) as start:
            r = self.client.post('/api/video/analyze', json={'url': 'https://youtu.be/abcdefghi01', 'mode': 'manual'})
        self.assertEqual(r.json()['job_id'], 42)
        self.assertEqual(start.call_args.kwargs['mode'], 'manual')
        self.assertFalse(video.settings()['smart'])

    def test_auth_required(self):
        app.dependency_overrides.pop(security.require_auth, None)
        self.assertEqual(self.client.put('/api/video/ai-settings', json={}).status_code, 401)
        self.assertEqual(self.client.post('/api/video/ai-test').status_code, 401)


class VideoAISummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunks_cover_full_input_and_vision_is_explicit(self):
        calls = []
        async def call(messages):
            calls.append(messages)
            return 'part ' + str(len(calls))
        transcript = 'A' * 8000 + 'B' * 8000 + 'C' * 21
        frame = {'at': 10, 'thumb': 'data:image/jpeg;base64,YWJj'}
        with patch.object(video, '_video_ai_call', side_effect=call), patch.object(video, 'settings', return_value={'ai_vision': True}):
            result, evidence = await video._video_ai_summary({'title': 'title', 'transcript': transcript}, [frame])
        self.assertEqual(result, 'part 4')
        self.assertEqual(evidence, {'source': 'transcript', 'segments': 3, 'frames_sent': 1})
        self.assertIn('A' * 8000, calls[0][1]['content'])
        self.assertIn('B' * 8000, calls[1][1]['content'])
        self.assertIn('C' * 21, calls[2][1]['content'])
        self.assertEqual(calls[-1][1]['content'][-1]['image_url']['url'], frame['thumb'])
        with patch.object(video, '_video_ai_call', side_effect=call), patch.object(video, 'settings', return_value={'ai_vision': False}):
            _, evidence = await video._video_ai_summary({'description': 'description'}, [frame])
        self.assertEqual(evidence['source'], 'description')
        self.assertEqual(evidence['frames_sent'], 0)
        self.assertIsInstance(calls[-1][1]['content'], str)

    async def test_http_auth_failure_is_explicit_and_does_not_expose_secret(self):
        seen = []
        def respond(request):
            seen.append(request)
            return httpx.Response(401, json={'error': 'SECRET-KEY'})
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        cfg = {'video_ai_api_key': 'SECRET-KEY', 'video_ai_base_url': 'https://api.example.test/v1', 'video_ai_model': 'model'}
        with patch.object(video.db, 'all_settings', return_value=cfg), patch.object(video.httpx, 'AsyncClient', return_value=client):
            with self.assertRaisesRegex(video.VideoError, 'video_ai_http_401') as err:
                await video.test_ai_connection()
        self.assertNotIn('SECRET-KEY', str(err.exception))
        self.assertEqual(seen[0].headers['Authorization'], 'Bearer SECRET-KEY')
        self.assertEqual(str(seen[0].url), 'https://api.example.test/v1/chat/completions')

    async def test_native_providers_text_and_image_requests(self):
        messages = [{'role': 'system', 'content': 'be accurate'}, {'role': 'user', 'content': [
            {'type': 'text', 'text': 'what is this?'},
            {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,YWJj'}},
        ]}]
        for provider, base, model, response_data in [
            ('openai_compatible', 'https://openrouter.ai/api/v1', 'openai/model', {'choices': [{'message': {'content': 'openai result'}}]}),
            ('gemini', 'https://generativelanguage.googleapis.com/v1beta', 'gemini-test', {'candidates': [{'content': {'parts': [{'text': 'gemini result'}]}}]}),
            ('anthropic', 'https://api.anthropic.com/v1', 'claude-test', {'content': [{'type': 'text', 'text': 'anthropic result'}]}),
        ]:
            with self.subTest(provider=provider):
                captured = []
                def respond(request):
                    captured.append(request)
                    return httpx.Response(200, json=response_data)
                client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
                cfg = {'video_ai_api_key': 'secret-key', 'video_ai_provider': provider, 'video_ai_base_url': base, 'video_ai_model': model}
                with patch.object(video.db, 'all_settings', return_value=cfg), patch.object(video.httpx, 'AsyncClient', return_value=client):
                    result = await video._video_ai_call(messages)
                self.assertEqual(result, provider.split('_')[0] + ' result')
                req, body = captured[0], json.loads(captured[0].content)
                if provider == 'openai_compatible':
                    self.assertEqual(str(req.url), base + '/chat/completions')
                    self.assertEqual(req.headers['authorization'], 'Bearer secret-key')
                    self.assertEqual(body['messages'], messages)
                elif provider == 'gemini':
                    self.assertEqual(str(req.url), base + '/models/gemini-test:generateContent')
                    self.assertEqual(req.headers['x-goog-api-key'], 'secret-key')
                    self.assertNotIn('secret-key', str(req.url))
                    self.assertEqual(body['system_instruction']['parts'][0]['text'], 'be accurate')
                    self.assertEqual(body['contents'][0]['parts'][1]['inline_data']['data'], 'YWJj')
                else:
                    self.assertEqual(str(req.url), base + '/messages')
                    self.assertEqual(req.headers['x-api-key'], 'secret-key')
                    self.assertEqual(req.headers['anthropic-version'], '2023-06-01')
                    self.assertEqual(body['system'], 'be accurate')
                    self.assertEqual(body['messages'][0]['content'][1]['source']['data'], 'YWJj')

    def test_bad_or_empty_provider_responses(self):
        for provider, response in [('gemini', {'candidates': []}), ('anthropic', {'content': []}), ('openai_compatible', {'choices': [{'message': {'content': None}}]})]:
            with self.subTest(provider=provider), self.assertRaisesRegex(video.VideoError, 'video_ai_bad_response'):
                video._provider_text(provider, response)

    async def test_youtube_reports_sampled_frames_and_does_not_claim_vision(self):
        cfg = {'frames': True, 'summarize': True, 'ai_key_set': True, 'ai_vision': False}
        meta = {'title': 'Video', 'transcript': '', 'description': 'Public description', 'notes': ['No public captions']}
        frames = [{'at': 1, 'thumb': 'data:image/jpeg;base64,YWJj'}]
        with patch.object(video, 'settings', return_value=cfg), \
             patch.object(video, 'ffmpeg_available', return_value=True), \
             patch.object(video, '_youtube_meta', new=AsyncMock(return_value=meta)), \
             patch.object(video, '_youtube_stream', new=AsyncMock(return_value={'url': 'https://cdn.googlevideo.com/test', 'duration': 100})), \
             patch.object(video, '_extract_frames', new=AsyncMock(return_value=(frames, {'count': 1}))), \
             patch.object(video, '_video_ai_call', new=AsyncMock(return_value='Detailed analysis')), \
             patch.object(video, 'analyse_text', return_value={}):
            report = await video._analyse('https://youtu.be/abcdefghi01')
        self.assertEqual(report['summary_engine'], 'video_ai')
        self.assertEqual(report['ai_evidence']['source'], 'description')
        self.assertEqual(report['ai_evidence']['frames_sent'], 0)
        self.assertIn('No public captions', report['notes'])
        self.assertTrue(any('مدل تصویری فعال نیست' in n for n in report['notes']))
        self.assertNotIn('googlevideo', str(report))

    async def test_youtube_missing_frames_fails_instead_of_title_summary(self):
        cfg = {'frames': True, 'summarize': True, 'ai_key_set': False, 'ai_vision': False}
        with patch.object(video, 'settings', return_value=cfg), \
             patch.object(video, 'ffmpeg_available', return_value=True), \
             patch.object(video, '_youtube_meta', new=AsyncMock(return_value={'title': 'Video'})), \
             patch.object(video, '_youtube_stream', new=AsyncMock(return_value={'url':'https://cdn.googlevideo.com/video','duration': 10})), \
             patch.object(video, '_extract_frames', new=AsyncMock(return_value=([], {}))), \
             self.assertRaisesRegex(video.VideoError, 'video_frames_unavailable'):
            await video._analyse('https://youtu.be/abcdefghi01')

    async def test_no_deadline_in_client(self):
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={'choices': [{'message': {'content': 'OK'}}]})))
        cfg = {'video_ai_api_key': 'SECRET-KEY', 'video_ai_base_url': 'https://api.example.test/v1', 'video_ai_model': 'model'}
        with patch.object(video.db, 'all_settings', return_value=cfg), patch.object(video.httpx, 'AsyncClient', return_value=client) as factory:
            await video.test_ai_connection()
        self.assertEqual(factory.call_args.kwargs['timeout'], httpx.Timeout(None))
        self.assertFalse(factory.call_args.kwargs['follow_redirects'])

    def test_youtube_host_must_be_genuine(self):
        self.assertEqual(video.classify_url('https://www.youtube.com/watch?v=abcdefghi01'), 'youtube')
        self.assertEqual(video.classify_url('https://youtu.be/abcdefghi01'), 'youtube')
        self.assertNotEqual(video.classify_url('https://youtube.com.evil.test/watch?v=abcdefghi01'), 'youtube')


if __name__ == '__main__':
    unittest.main()
