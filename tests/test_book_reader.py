"""Regression tests for the self-contained, chapter-mounted HTML reader."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

from bs4 import BeautifulSoup

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from book_reader_generator import make_reader  # noqa: E402
import merge_and_build  # noqa: E402


class OfflineReaderTests(unittest.TestCase):
    def generate(self, source):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        root = Path(workspace.name)
        input_file, output = root / "input.html", root / "book.html"
        input_file.write_text(source, encoding="utf-8")
        stats = make_reader(input_file, output)
        soup = BeautifulSoup(output.read_text(encoding="utf-8"), "html.parser")
        payload = json.loads(soup.find(id="bookPayload").string)
        chapters = [
            json.loads(soup.find(id=f"chapter-json-{index}").string)
            for index in range(len(payload["chapters"]))
        ]
        return root, stats, soup, payload, chapters

    def test_chapters_only_mount_in_reader_and_lookups_are_embedded(self):
        root, stats, soup, payload, contents = self.generate(
            '<!doctype html><html lang="en"><head><title>Test</title></head>'
            '<body><h1>Chapter 1</h1><p>first text</p>'
            '<h1>Chapter 2</h1><p>second text</p></body></html>'
        )
        self.assertEqual(stats["chapters"], 2)
        self.assertEqual(soup.html.get("lang"), "en")
        self.assertFalse(soup.select("#chapterBody p"))
        self.assertEqual(payload["chapters"][1]["title"], "Chapter 2")
        self.assertIn("first text", contents[0])
        self.assertNotIn("second text", contents[0])
        self.assertIn("second text", contents[1])
        self.assertEqual(soup.select_one('meta[name="book-reader"]')['content'], "v1")
        self.assertFalse(soup.select('script[src]'))

    def test_font_weight_and_true_white_theme_controls(self):
        _, _, soup, _, _ = self.generate('<html><body><h1>正文</h1><p>段落</p></body></html>')
        self.assertEqual([x.get("value") for x in soup.select("#weightSelect option")],
                         ["300", "400", "500", "600", "700"])
        self.assertIsNotNone(soup.select_one('#themeSelect option[value="white"]'))
        style = soup.style.string
        script = soup.find_all("script")[-1].string
        self.assertIn("--paper:#fff;", style)
        self.assertIn("data-theme=white", style)
        self.assertIn("font-weight:var(--reader-weight)", style)
        self.assertIn("o.weight=Number(e.target.value)", script)
        self.assertIn("localStorage", script)

    def test_pandoc_footnotes_create_superscript_and_payload(self):
        _, stats, _, payload, chapters = self.generate(
            '<html><body><h1>史传</h1><p>正文'
            '<a href="#fn1" role="doc-noteref"><sup>1</sup></a>。</p>'
            '<section class="footnotes" role="doc-endnotes">'
            '<ol><li id="fn1"><p>注释全文。 '
            '<a href="#fnref1" role="doc-backlink">↩</a></p></li></ol>'
            '</section></body></html>'
        )
        self.assertEqual(stats["notes"], 1)
        self.assertIn('data-note="fn1"', chapters[0])
        self.assertIn("注释全文。", payload["notes"]["fn1"])
        self.assertNotIn("注释全文。", chapters[0])

    def test_classical_commentary_quote_with_marker(self):
        _, stats, _, payload, chapters = self.generate(
            '<html><body><h1>魏书武帝纪</h1>'
            '<p>太祖举兵【一】，众归之。</p>'
            '<blockquote><p>【一】《魏书》曰：兵三十余万。</p></blockquote>'
            '</body></html>'
        )
        self.assertEqual(stats["notes"], 1)
        self.assertIn('data-note="一"', chapters[0])
        self.assertIn("三十余万", payload["notes"]["一"])
        self.assertNotIn("兵三十余万", chapters[0])

    def test_image_assets_embedded_not_fetch_dependent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "images").mkdir()
            (root / "images/a.png").write_bytes(b"\x89PNG\r\n\x1a\nsample")
            source, target = root / "input.html", root / "reader.html"
            source.write_text('<html><body><h1>A</h1><img src="images/a.png"/></body></html>',
                              encoding="utf-8")
            stats = make_reader(source, target)
            self.assertEqual(stats["embedded_images"], 1)
            soup = BeautifulSoup(target.read_text(encoding="utf-8"), "html.parser")
            content = json.loads(soup.find(id="chapter-json-0").string)
            self.assertIn('src="data:image/png;base64,', content)

    def test_build_stage_does_not_change_static_doc_html(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            doc = root / "book_doc.html"
            doc.write_text('<html><body><p>Keep for EPUB/PDF/DOCX</p></body></html>',
                           encoding="utf-8")
            reader = root / "book.html"
            reader.write_text('<html><head><title>B</title></head>'
                              '<body><h1>First</h1><p>Hello</p></body></html>', encoding="utf-8")
            self.assertTrue(merge_and_build.add_toc(str(root)))
            self.assertIn('name="book-reader" content="v1"',
                          reader.read_text(encoding="utf-8"))
            self.assertIn("Keep for EPUB/PDF/DOCX", doc.read_text(encoding="utf-8"))
            self.assertTrue(merge_and_build.add_toc(str(root)))


if __name__ == "__main__":
    unittest.main()
