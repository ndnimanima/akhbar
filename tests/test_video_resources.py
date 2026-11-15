"""Video resource controls do not impose a timeout or strand cancelled jobs."""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from app import video


class ResourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_analyses_run_one_at_a_time_and_waiting_job_can_cancel(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        active = 0
        peak = 0
        starts = []

        async def analyse_inner(url, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            starts.append(url)
            entered.set()
            try:
                await release.wait()
                return {'url': url}
            finally:
                active -= 1

        with patch.object(video, '_analyse', analyse_inner):
            first = asyncio.create_task(video.analyse('first'))
            await entered.wait()
            second = asyncio.create_task(video.analyse('second'))
            third = asyncio.create_task(video.analyse('third'))
            await asyncio.sleep(0)
            self.assertEqual(starts, ['first'])
            second.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await second
            release.set()
            self.assertEqual((await first)['url'], 'first')
            self.assertEqual((await third)['url'], 'third')
            self.assertEqual(peak, 1)

    async def test_subprocess_is_reaped_on_cancellation(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'pid'
            script = ('import os,sys,time; '
                      'open(sys.argv[1],"w").write(str(os.getpid())); time.sleep(60)')
            task = asyncio.create_task(video._run([sys.executable, '-c', script, str(marker)]))
            for _ in range(100):
                if marker.exists() and marker.read_text():
                    break
                await asyncio.sleep(.01)
            self.assertTrue(marker.exists())
            pid = int(marker.read_text())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    async def test_immediate_cancellation_cleans_upload_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            upload = Path(tmp) / 'upload'
            upload.mkdir()
            (upload / 'video.mp4').write_bytes(b'data')
            with patch.object(video.db, 'execute', return_value=123456), patch.object(video, '_set_job') as update:
                job_id = video.start_analysis('upload:video.mp4', local_file=str(upload / 'video.mp4'), cleanup_dir=str(upload))
                self.assertTrue(video.cancel_analysis(job_id))
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                self.assertFalse(upload.exists())
                self.assertNotIn(job_id, video._TASKS)
                self.assertEqual(update.call_args.kwargs['status'], 'cancelled')

    async def test_fetch_text_stops_reading_after_limit(self):
        reads = []

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                for i in range(20):
                    reads.append(i)
                    yield b'x' * 16384

        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=Stream())))
        with patch.object(video.httpx, 'AsyncClient', return_value=client):
            result = await video._fetch_text('https://example.test/', limit=1000)
        self.assertEqual(result, 'x' * 1000)
        self.assertLess(len(reads), 20)


class UploadCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_upload_removes_incomplete_temp_file(self):
        from app.main import video_upload
        class Request:
            async def stream(self):
                yield b'data'
                raise asyncio.CancelledError()
        with tempfile.TemporaryDirectory() as tmp:
            upload_dir = Path(tmp) / 'upload'
            upload_dir.mkdir()
            with patch('app.main.tempfile.mkdtemp', return_value=str(upload_dir)):
                with self.assertRaises(asyncio.CancelledError):
                    await video_upload(Request(), filename='test.mp4', title='', _=None)
            self.assertFalse(upload_dir.exists())


if __name__ == '__main__':
    unittest.main()
