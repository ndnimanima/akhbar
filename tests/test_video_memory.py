"""Regression tests for bounded-memory MP4 probing and real frame extraction."""
import asyncio
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.video import _extract_frames, _mp4_probe


def box(kind, payload, extended=False):
    if extended:
        return struct.pack('>I4sQ', 1, kind, len(payload) + 16) + payload
    return struct.pack('>I4s', len(payload) + 8, kind) + payload


def metadata():
    mvhd = box(b'mvhd', b'\0' * 12 + struct.pack('>II', 1000, 12000))
    tkhd = box(b'tkhd', b'\0' * 76 + struct.pack('>II', 640 << 16, 360 << 16))
    stsd = box(b'stsd', b'\0' * 12 + b'avc1')
    return box(b'moov', mvhd + box(b'trak', tkhd + box(b'mdia', box(b'minf', box(b'stbl', stsd)))), extended=True)


class VideoProbeTests(unittest.TestCase):
    def test_large_mdat_and_moov_at_end_with_bounded_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'large.mp4'
            # Sparse file keeps the test fast while exercising a >256 MiB offset.
            media_size = 300 * 1024 * 1024
            with path.open('wb') as f:
                f.write(struct.pack('>I4s', media_size, b'mdat'))
                f.seek(media_size)
                f.write(metadata())
            original_open = open
            sizes = []

            class ReadMonitor:
                def __init__(self, f): self.f = f
                def __enter__(self): return self
                def __exit__(self, *args): return self.f.__exit__(*args)
                def seek(self, *args): return self.f.seek(*args)
                def read(self, n=-1):
                    sizes.append(n)
                    if n < 0 or n > 16:
                        raise AssertionError(f'unbounded read: {n}')
                    return self.f.read(n)

            def monitored_open(name, mode):
                return ReadMonitor(original_open(name, mode))

            with patch('builtins.open', monitored_open):
                info = _mp4_probe(path)
            self.assertEqual(info['duration'], 12)
            self.assertEqual((info['width'], info['height']), (640, 360))
            self.assertEqual(info['video_codec'], 'avc1')
            self.assertGreater(info['size'], media_size)
            self.assertLessEqual(max(sizes), 16)

    def test_truncated_box_is_handled(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'broken.mp4'
            path.write_bytes(struct.pack('>I4s', 200, b'moov') + b'broken')
            result = _mp4_probe(path)
            self.assertEqual(result['duration'], 0)
            self.assertEqual(result['size'], 14)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg not installed')
    def test_real_video_probe_and_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'real.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                            'color=c=blue:size=320x240:rate=10', '-t', '3',
                            '-c:v', 'mpeg4', str(path)], check=True)
            info = _mp4_probe(path)
            self.assertAlmostEqual(info['duration'], 3, delta=.2)
            self.assertEqual((info['width'], info['height']), (320, 240))
            frames, summary = asyncio.run(_extract_frames(str(path), info['duration']))
            self.assertEqual(summary['count'], 6)
            self.assertTrue(all(f['thumb'].startswith('data:image/jpeg;base64,') for f in frames))


if __name__ == '__main__':
    unittest.main()
