"""Stream resolution and frame analysis; no real YouTube access needed."""
import unittest
from unittest.mock import AsyncMock, patch
from app import video


class YouTubeStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_restricts_origin_and_keeps_only_metadata(self):
        output = '120\t640\t360\thttps://r1.googlevideo.com/videoplayback?signature=private\n'
        with patch.object(video, '_run', new=AsyncMock(return_value=output)) as run:
            stream = await video._youtube_stream('abcdefghijk')
        self.assertEqual(stream['duration'], 120)
        self.assertEqual(stream['width'], 640)
        self.assertIn('https://www.youtube.com/watch?v=abcdefghijk', run.call_args.args[0])
        self.assertIn('--ignore-config', run.call_args.args[0])
        for url in ['http://r1.googlevideo.com/x', 'https://googlevideo.com.evil.test/x',
                    'https://localhost/x', 'https://user@googlevideo.com/x', 'https://googlevideo.com:bad/x']:
            with self.subTest(url=url), patch.object(video, '_run', new=AsyncMock(return_value='1\t1\t1\t'+url)):
                with self.assertRaisesRegex(video.VideoError, 'video_stream_unavailable'):
                    await video._youtube_stream('abcdefghijk')

    async def test_unavailable_stream_does_not_return_metadata_summary(self):
        with patch.object(video, 'settings', return_value={'frames':True}), \
             patch.object(video, 'ffmpeg_available', return_value=True), \
             patch.object(video, '_youtube_meta', new=AsyncMock(return_value={'title':'title only'})), \
             patch.object(video, '_youtube_stream', new=AsyncMock(side_effect=video.VideoError('video_stream_unavailable'))):
            with self.assertRaisesRegex(video.VideoError, 'video_stream_unavailable'):
                await video._analyse('https://youtu.be/abcdefghijk')

    async def test_vision_model_receives_sampled_frames(self):
        frame={'at': 2, 'thumb':'data:image/jpeg;base64,YWJj'}
        with patch.object(video, 'settings', return_value={'ai_vision':True}), \
             patch.object(video, '_video_ai_call', new=AsyncMock(return_value='Visual analysis')) as call:
            result, evidence = await video._video_ai_summary({'title':'A video'}, [frame])
        self.assertEqual(result, 'Visual analysis')
        self.assertEqual(evidence['frames_sent'], 1)
        self.assertEqual(call.call_args.args[0][-1]['content'][-1]['image_url']['url'], frame['thumb'])


class OnlineVideoTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_http_video_is_sampled_without_downloading_whole_file_to_python(self):
        import os
        import shutil
        import subprocess
        import tempfile
        import threading
        from functools import partial
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
        from pathlib import Path

        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            self.skipTest('ffmpeg not installed')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'sample.mp4'
            subprocess.run([ffmpeg, '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                            'testsrc=size=160x120:rate=4', '-t', '12', '-c:v', 'mpeg4',
                            '-movflags', '+faststart', str(path)], check=True)
            requests = []
            class Handler(SimpleHTTPRequestHandler):
                def do_GET(self):
                    requests.append(self.path)
                    super().do_GET()
                def log_message(self, *args): pass
            server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=folder))
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            url = f'http://127.0.0.1:{server.server_port}/sample.mp4'
            cfg = {'frames': True, 'summarize': False, 'ai_key_set': False, 'ai_vision': False}
            try:
                with patch.object(video, 'settings', return_value=cfg), \
                     patch.object(video, '_youtube_meta', new=AsyncMock(return_value={'title':'HTTP test'})), \
                     patch.object(video, '_youtube_stream', new=AsyncMock(return_value={'url':url, 'duration':12})), \
                     patch.object(video, 'analyse_text', return_value={}), \
                     patch.object(video, '_download', new=AsyncMock(side_effect=AssertionError('unexpected download'))):
                    report = await video._analyse('https://youtu.be/abcdefghijk')
                self.assertEqual(len(report['frames']), video.FRAME_COUNT)
                self.assertGreaterEqual(len(requests), 1)
                self.assertEqual(report['frames_meta']['count'], video.FRAME_COUNT)
                self.assertNotIn(url, str(report))
            finally:
                server.shutdown(); server.server_close(); thread.join()

if __name__ == '__main__': unittest.main()
