"""README artwork must work as local, accessible GitHub assets."""
from pathlib import Path
import re
import unittest

from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parents[1]


class ReadmeArtworkTests(unittest.TestCase):
    def test_linked_local_images_and_docs_exist(self):
        readme = (ROOT / 'README.md').read_text(encoding='utf-8')
        for match in re.finditer(r'(?:src|href)="(docs/[^"#]+)"|\]\((docs/[^)#]+)\)', readme):
            target = next(value for value in match.groups() if value)
            self.assertTrue((ROOT / target).exists(), target)
        self.assertIn('data/rozaneh.db', readme)  # Keep the existing public-release warning visible.
        self.assertEqual(len(re.findall(r'!\[[^\]]*\]\(docs/shots/', readme)) + readme.count('src="docs/shots/'), 3)
        for username in ('ndnima', 'alireza-bokharaei', 'ostovar04'):
            self.assertIn('https://github.com/' + username, readme)
            self.assertIn('docs/images/creator-' + username + '.gif', readme)
        self.assertIn('docs/images/workflow-still.png', readme)

    def test_animated_assets_really_contain_distinct_frames(self):
        for name in ('workflow.gif', 'creator-ndnima.gif', 'creator-alireza-bokharaei.gif', 'creator-ostovar04.gif'):
            with self.subTest(name=name), Image.open(ROOT / 'docs/images' / name) as image:
                self.assertGreater(image.n_frames, 1)
                self.assertEqual(image.info.get('loop'), 0)
                image.seek(0)
                first = image.convert('RGB')
                image.seek(image.n_frames // 2)
                self.assertIsNotNone(ImageChops.difference(first, image.convert('RGB')).getbbox())
