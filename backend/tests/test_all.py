import asyncio
import base64
import json
import os
import unittest
from pathlib import Path
from unittest import mock

import fitz
import httpx
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.api.schemas import FullPageRequest
from app.clients.chandra import OCR_LAYOUT_PROMPT, RETRY_TEMPERATURE, html_to_text, normalize_html, to_html
from app.config import QUEUES, ROOT, Settings, load_settings
from app.pipeline import fullpage as flow
from app.spell.checker import SpellChecker, load_replacements
from app.spell.lexicon import Lexicon, unaccent
from app.spell.unknown import build_notice, find_unknown_words
from app.storage import db
from app.worker import tasks
from app.worker.celery_app import celery_app

LEX = Lexicon.from_dictionaries(str(ROOT / "data" / "dictionaries"))


def run(coro):
    return asyncio.run(coro)


class FakeFixer:
    """ProtonXClient giả: answer(texts) trả text đã sửa hoặc None."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    async def correct(self, texts):
        self.calls.append(list(texts))
        return self.answer(texts) if callable(self.answer) else self.answer


def swap(wrong, right):
    return lambda texts: [t.replace(wrong, right) for t in texts]


class HtmlToText(unittest.TestCase):
    def test_tables_figures_and_entities(self):
        raw = (
            '<div data-label="Text"><p>Xin&nbsp;ch&agrave;o</p></div>'
            '<div data-label="Figure"><img alt="invented 12m"/></div>'
            "<table><tr><td>39</td><td>111</td></tr></table>m<sup>2</sup>"
        )
        text = html_to_text(raw)
        self.assertIn("Xin chào", text)
        self.assertIn("39 | 111", text)
        self.assertNotIn("invented", text)
        self.assertIn("m2", text)


class ChandraHtmlOnly(unittest.TestCase):
    def test_html_is_detected(self):
        self.assertEqual(normalize_html('<div data-label="Text"><p>A</p></div>'), ('<div data-label="Text"><p>A</p></div>', True))
        self.assertEqual(normalize_html("```html\n<p>A</p>\n```"), ("<p>A</p>", True))
        self.assertEqual(normalize_html("  "), ("", True))
        for bad in ('<analyze>[{"text": "A"}]</analyze>', '[{"titles": "A"}]', '{"text": "A"}', "# Tiêu đề", "Chữ thường", "<unknown>x"):
            self.assertFalse(normalize_html(bad)[1], bad)

    def test_non_html_is_converted_to_html(self):
        md = to_html("# Tiêu đề\n\n- Dòng **một**\n- Dòng <hai>\n\n| a | b |\n|---|---|")
        self.assertEqual(md, "<p>Tiêu đề</p>\n<p>Dòng một<br>Dòng &lt;hai&gt;</p>\n<p>| a | b |</p>")
        self.assertTrue(normalize_html(md)[1])
        js = to_html('<analyze>[{"text": "\'Việc thực hiện nghị quyết\'"}]</analyze>')
        self.assertTrue(normalize_html(js)[1])
        self.assertIn("Việc thực hiện nghị quyết", html_to_text(js))

    def test_retries_until_html(self):
        mocks = Mocks(['<analyze>[{"titles": "x"}]', "# Markdown", '<div data-label="Text"><p>Xin chào</p></div>'])
        with make_client(mocks) as client:
            r = client.post("/api/ocr-fullpage/sync", headers=KEY, files={"file": ("a.png", make_png(), "image/png")})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["text"], "Xin chào")
        self.assertEqual([q["temperature"] for q in mocks.chandra_requests], [0, RETRY_TEMPERATURE, RETRY_TEMPERATURE])

    def test_gives_html_when_retries_run_out(self):
        mocks = Mocks(["## Quyết định\n\nĐiều **1**. Giao đất"])
        with make_client(mocks, chandra_html_retries=1) as client:
            r = client.post("/api/ocr-fullpage/sync", headers=KEY, files={"file": ("a.png", make_png(), "image/png")})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(mocks.chandra_requests), 2)
        self.assertEqual(r.json()["text"], "Quyết định\n\nĐiều 1. Giao đất")

    def test_page_keeps_raw_chandra_output_and_logs_it(self):
        raw = '```html\n<div data-bbox="1 2 3 4" data-label="Text"><p>Xin chào</p></div>\n```'
        mocks = Mocks(raw)
        with make_client(mocks, log_full_output=True) as client, self.assertLogs("ocr", "INFO") as logs:
            r = client.post("/api/ocr-fullpage/sync", headers=KEY, files={"file": ("a.png", make_png(), "image/png")})
        self.assertEqual(r.status_code, 200, r.text)
        page = r.json()["pages"][0]
        self.assertEqual(page["chandra_raw"], raw)
        self.assertEqual(page["html"], "<p>Xin chào</p>")
        out = "\n".join(logs.output)
        self.assertIn("[page 1] Chandra output, attempt 1/3", out)
        self.assertIn(raw, out)

    def test_text_file_page_has_no_raw_chandra_output(self):
        with make_client(Mocks()) as client:
            r = client.post("/api/ocr-fullpage/sync", headers=KEY, files={"file": ("a.txt", "Xin chào".encode(), "text/plain")})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(r.json()["pages"][0]["chandra_raw"])

    def test_protonx_logs_every_text_in_full(self):
        from app.clients.protonx import ProtonXClient

        def handler(req):
            return httpx.Response(200, json={"texts": ["Đất nước", "giữ nguyên"]})

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                return await ProtonXClient(http, "http://x/spellfix/correct", 5, 1, log_output=True).correct(["Đất nuớc", "giữ nguyên"])

        with self.assertLogs("ocr.protonx", "INFO") as logs:
            self.assertEqual(asyncio.run(run()), ["Đất nước", "giữ nguyên"])
        out = "\n".join(logs.output)
        self.assertIn("ProtonX 1/2 changed\n  in : Đất nuớc\n  out: Đất nước", out)
        self.assertIn("ProtonX 2/2 unchanged", out)


class InlineFormatting(unittest.TestCase):
    HTML = (
        '<div data-label="Page-Header"><p style="text-align: center;"><b>CỘNG HÒA XÃ HỘI</b><br><b><u>Độc lập</u></b></p></div>'
        '<p align="right"><i>Hà Nội, ngày 1</i></p>'
        '<h2><b>QUYẾT ĐỊNH</b></h2>'
        '<p><b>Điều 1.</b> Người đưoc giao 120 m<sup>2</sup></p>'
        '<table><tr><th>Tên</th></tr><tr><td style="text-align:center"><strong>Phòng A</strong></td></tr></table>'
        '<ul><li><em>Mục</em> một</li></ul>'
    )

    def test_layout_keeps_bold_italic_and_alignment(self):
        from app.pipeline.layout import layout_html, parse_layout

        page = layout_html(parse_layout(self.HTML))
        self.assertIn('<p style="text-align:center"><b>CỘNG HÒA XÃ HỘI</b><br><b><u>Độc lập</u></b></p>', page)
        self.assertIn('<p style="text-align:right"><i>Hà Nội, ngày 1</i></p>', page)
        self.assertIn("<h2>QUYẾT ĐỊNH</h2>", page)
        self.assertIn("<p><b>Điều 1.</b> Người đưoc giao 120 m<sup>2</sup></p>", page)
        self.assertIn('<td style="text-align:center"><b>Phòng A</b></td>', page)
        self.assertIn("<li><i>Mục</i> một</li>", page)

    def test_spelling_fix_keeps_formatting(self):
        from app.pipeline.layout import layout_html, layout_text, parse_layout

        blocks = parse_layout("<p><b>Người đưoc giao</b> việc</p>")
        fixed = layout_text(blocks).replace("đưoc", "được")
        self.assertEqual(layout_html(blocks, fixed), "<p><b>Người được giao</b> việc</p>")

    def test_remap_marks_follows_changed_text(self):
        from app.pipeline.layout import remap_marks

        self.assertEqual(remap_marks("ab cd", "abx cd", [[0, 2, "b"]]), [[0, 3, "b"]])
        self.assertEqual(remap_marks("ab cd", "ab cdx", [[3, 5, "i"]]), [[3, 6, "i"]])

    def test_sync_ocr_html_keeps_bold_after_spellcheck(self):
        mocks = Mocks('<div data-label="Text"><p style="text-align:center"><b>Người đưoc giao</b></p></div>',
                      spellfix=swap("đưoc", "được"))
        with make_client(mocks) as client:
            r = client.post("/api/ocr-fullpage/sync", headers=KEY, files={"file": ("a.png", make_png(), "image/png")})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["pages"][0]["html"], '<p style="text-align:center"><b>Người được giao</b></p>')

    def test_docx_runs_keep_bold_caps_and_alignment(self):
        import io

        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH

        from app.inputs.textfile import read_text_pages
        from app.pipeline.layout import layout_html

        d = Document()
        p = d.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.add_run("Quốc hiệu").bold = True
        caps = p.add_run(" độc lập")
        caps.font.all_caps = True
        q = d.add_paragraph("Nội dung ")
        q.add_run("nghiêng").italic = True
        t = d.add_table(rows=1, cols=1)
        t.cell(0, 0).paragraphs[0].add_run("Ô đậm").bold = True
        buf = io.BytesIO()
        d.save(buf)
        path = Path(os.environ["TEMP_DIR"]) / "dinh-dang.docx"
        path.write_bytes(buf.getvalue())
        pages, problem = read_text_pages(path)
        self.assertEqual(problem, "")
        self.assertEqual(pages[0]["text"].split("\n")[0], "Quốc hiệu ĐỘC LẬP")
        page = layout_html(pages[0]["layout"])
        self.assertIn('<p style="text-align:center"><b>Quốc hiệu</b> ĐỘC LẬP</p>', page)
        self.assertIn("<p>Nội dung <i>nghiêng</i></p>", page)
        self.assertIn("<td><b>Ô đậm</b></td>", page)


class Export(unittest.TestCase):
    PAGES = [
        '<p style="text-align:center"><b>CỘNG HÒA XÃ HỘI CHỦ NGHĨA VIỆT NAM</b></p>'
        '<table border="1"><caption>Bảng 1</caption><tr><th rowspan="2">STT</th><th colspan="2">Kết quả</th></tr>'
        '<tr><th>Đạt</th><th>Không</th></tr><tr><td>1</td><td><i>12</i></td><td>1</td></tr></table>',
        "<h1>PHỤ LỤC</h1><ul><li>Mục một</li></ul>",
    ]

    def test_export_docx_rebuilds_tables_and_formatting(self):
        import io

        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH

        with make_client(Mocks()) as client:
            r = client.post("/api/export/docx", headers=KEY, json={"title": "Quyết định 1", "pages": self.PAGES})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("filename*=UTF-8''Quy%E1%BA%BFt%20%C4%91%E1%BB%8Bnh%201.docx", r.headers["content-disposition"])
        doc = Document(io.BytesIO(r.content))
        first = doc.paragraphs[0]
        self.assertEqual(first.text, "CỘNG HÒA XÃ HỘI CHỦ NGHĨA VIỆT NAM")
        self.assertEqual(first.alignment, WD_ALIGN_PARAGRAPH.CENTER)
        self.assertTrue(first.runs[0].bold)
        table = doc.tables[0]
        self.assertEqual((len(table.rows), len(table.columns)), (3, 3))
        self.assertIs(table.cell(0, 0)._tc, table.cell(1, 0)._tc)   # STT gộp 2 hàng
        self.assertIs(table.cell(0, 1)._tc, table.cell(0, 2)._tc)   # Kết quả gộp 2 cột
        self.assertTrue(table.cell(2, 1).paragraphs[0].runs[0].italic)
        self.assertIn("PHỤ LỤC", [p.text for p in doc.paragraphs])
        self.assertIn('w:type="page"', doc.element.xml)   # mỗi trang gốc một trang

    def test_export_pdf_has_one_page_per_source_page(self):
        import pymupdf

        with make_client(Mocks()) as client:
            r = client.post("/api/export/pdf", headers=KEY, json={"title": "QĐ", "pages": self.PAGES})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers["content-type"], "application/pdf")
        pdf = pymupdf.open("pdf", r.content)
        self.assertEqual(pdf.page_count, 2)
        self.assertIn("CỘNG HÒA XÃ HỘI CHỦ NGHĨA VIỆT NAM", pdf[0].get_text())
        self.assertIn("Không", pdf[0].get_text())
        self.assertIn("PHỤ LỤC", pdf[1].get_text())

    def test_export_pdf_without_font_files_keeps_vietnamese(self):
        import pymupdf

        from app.pipeline.export import page_layouts, to_pdf

        pdf = pymupdf.open("pdf", to_pdf(page_layouts(["<p><b>Người được giao</b> ữ ỵ</p>"]), "", None))
        self.assertIn("Người được giao ữ ỵ", pdf[0].get_text())

    def test_export_rejects_unknown_format_and_needs_login(self):
        with make_client(Mocks()) as client:
            self.assertEqual(client.post("/api/export/odt", headers=KEY, json={"pages": ["<p>a</p>"]}).status_code, 422)
            self.assertEqual(client.post("/api/export/docx", json={"pages": ["<p>a</p>"]}).status_code, 401)
            self.assertEqual(client.post("/api/export/docx", headers=KEY, json={"pages": []}).status_code, 422)

    def test_export_drops_unsafe_html(self):
        import pymupdf

        with make_client(Mocks()) as client:
            r = client.post("/api/export/pdf", headers=KEY,
                            json={"pages": ['<p>An toàn</p><script>alert(1)</script><img src="http://x/y.png">']})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(pymupdf.open("pdf", r.content)[0].get_text().strip(), "An toàn")


class Dictionaries(unittest.TestCase):
    def test_all_three_dictionaries_are_loaded(self):
        self.assertEqual(set(LEX.sources), {"hongocduc", "tudientv", "wiktionary"})
        self.assertTrue(all(n > 10_000 for n in LEX.sources.values()), LEX.sources)

    def test_dictionaries_can_be_selected(self):
        one = Lexicon.from_dictionaries(str(ROOT / "data" / "dictionaries"), ("tudientv",))
        self.assertEqual(set(one.sources), {"tudientv"})
        self.assertLess(len(one), len(LEX))

    def test_missing_dictionary_is_an_error(self):
        with self.assertRaises(FileNotFoundError):
            Lexicon.from_dictionaries(str(ROOT / "data" / "dictionaries"), ("nope",))

    def test_extra_words_file_is_trusted(self):
        extra = ROOT / "tests" / "_extra.txt"
        extra.write_text("quạc quạc\n", encoding="utf-8")
        try:
            lex = Lexicon.from_dictionaries(str(ROOT / "data" / "dictionaries"), ("tudientv",), str(extra))
        finally:
            extra.unlink()
        self.assertIn("quạc", lex)

    def test_diacritic_candidates(self):
        self.assertIn("thực", [c for c, _ in LEX.candidates("thưc")][:4])
        self.assertEqual(unaccent("Đường"), "duong")
        self.assertIn("thực", LEX)
        self.assertNotIn("thưc", LEX)


class DictionaryMode(unittest.TestCase):
    def setUp(self):
        self.sc = SpellChecker(LEX, None, mode="dict")

    def test_fixes_with_context_and_keeps_protected_tokens(self):
        text = "Việc thưc hiện nghị quyêt của UBND số 123/QĐ-UBND ngày 12/05/2021"
        out, corr = run(self.sc.check(text))
        self.assertEqual(out, "Việc thực hiện nghị quyết của UBND số 123/QĐ-UBND ngày 12/05/2021")
        self.assertEqual({c["by"] for c in corr}, {"dict"})

    def test_chinh_sach_uses_diacritic_candidates_first(self):
        out, _ = run(self.sc.check("Hồ sơ doanh nghiêp và chính sach phát triển"))
        self.assertEqual(out, "Hồ sơ doanh nghiệp và chính sách phát triển")

    def test_case_is_preserved(self):
        out, _ = run(self.sc.check("Doanh nghiêp và NGHIÊP"))
        self.assertTrue(out.startswith("Doanh nghiệp"))

    def test_unrelated_text_is_left_alone(self):
        out, corr = run(self.sc.check("zzz xyz abcd"))
        self.assertEqual((out, corr), ("zzz xyz abcd", []))

    def test_off_mode_changes_nothing(self):
        out, corr = run(SpellChecker(LEX, None, mode="off").check("thưc hiện"))
        self.assertEqual((out, corr), ("thưc hiện", []))

    def test_custom_replacements_apply_first(self):
        rules = load_replacements(str(ROOT / "data" / "spell_dict.csv"))
        out, corr = run(SpellChecker(LEX, None, mode="dict", replacements=rules).check("Dòng đầu\nuy ban nhan dan tinh"))
        self.assertTrue(out.split("\n")[1].startswith("UBND"))
        self.assertEqual((corr[0]["by"], corr[0]["line"]), ("replace", 2))


class ProtonXMode(unittest.TestCase):
    def test_protonx_fix_is_applied_and_only_suspect_lines_are_sent(self):
        fixer = FakeFixer(swap("đưoc", "được"))
        out, corr = run(SpellChecker(LEX, fixer, mode="both").check("Người đưoc giao đất\nViệc thực hiện nghị quyết"))
        self.assertEqual(out, "Người được giao đất\nViệc thực hiện nghị quyết")
        self.assertEqual(corr, [{"line": 1, "wrong": "đưoc", "right": "được", "by": "protonx"}])
        self.assertEqual(fixer.calls, [["Người đưoc giao đất"]])

    def test_protonx_cannot_invent_words(self):
        fixer = FakeFixer(lambda texts: ["Người quốc giao đất xyz"])
        out, corr = run(SpellChecker(LEX, fixer, mode="both").check("Người đưoc giao đất"))
        self.assertNotIn("quốc", out)
        self.assertNotIn("xyz", out)
        self.assertNotIn("protonx", {c["by"] for c in corr})

    def test_protonx_cannot_change_a_correct_word_beyond_diacritics(self):
        fixer = FakeFixer(lambda texts: ["giao nước cho dân"])
        out, _ = run(SpellChecker(LEX, fixer, mode="both", review="all").check("giao đất cho dân"))
        self.assertEqual(out, "giao đất cho dân")

    def test_numbers_punctuation_and_rewrites_are_kept_from_the_original(self):
        fixer = FakeFixer(lambda texts: ["Điều 13. Người được giao, đất ở"])
        out, corr = run(SpellChecker(LEX, fixer, mode="both").check("Điều 12: Người đưoc giao đất"))
        self.assertEqual(out, "Điều 12: Người được giao đất")
        self.assertEqual([(c["wrong"], c["right"], c["by"]) for c in corr], [("đưoc", "được", "protonx")])

    def test_case_is_kept_when_protonx_changes_it(self):
        fixer = FakeFixer(lambda texts: ["người được giao đất"])
        out, _ = run(SpellChecker(LEX, fixer, mode="both").check("NGƯỜI ĐƯOC giao đất"))
        self.assertEqual(out, "NGƯỜI ĐƯỢC giao đất")

    def test_dictionary_handles_suspects_protonx_left_alone(self):
        fixer = FakeFixer(lambda texts: list(texts))
        out, corr = run(SpellChecker(LEX, fixer, mode="both").check("Việc thưc hiện nghị quyêt"))
        self.assertEqual(out, "Việc thực hiện nghị quyết")
        self.assertEqual({c["by"] for c in corr}, {"dict"})

    def test_falls_back_to_dictionary_when_protonx_fails(self):
        out, corr = run(SpellChecker(LEX, FakeFixer(None), mode="both").check("Việc thưc hiện nghị quyêt"))
        self.assertEqual(out, "Việc thực hiện nghị quyết")
        self.assertEqual({c["by"] for c in corr}, {"dict"})

    def test_review_all_sends_clean_lines_too(self):
        fixer = FakeFixer(lambda texts: list(texts))
        run(SpellChecker(LEX, fixer, mode="both", review="all").check("Dòng một hoàn toàn đúng\nDòng hai"))
        self.assertEqual(len(fixer.calls[0]), 2)

    def test_dict_mode_never_calls_protonx(self):
        fixer = FakeFixer(swap("đưoc", "được"))
        run(SpellChecker(LEX, fixer, mode="dict").check("Người đưoc giao đất"))
        self.assertEqual(fixer.calls, [])


class ProtonXClientTest(unittest.TestCase):
    def client(self, handler):
        from app.clients.protonx import ProtonXClient

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return ProtonXClient(http, "http://spellfix/spellfix/correct", 5, 2)

    def test_sends_texts_and_returns_the_answer(self):
        seen = []

        def handler(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"texts": ["b", "d"]})

        self.assertEqual(run(self.client(handler).correct(["a", "c"])), ["b", "d"])
        self.assertEqual(seen, [{"texts": ["a", "c"]}])

    def test_wrong_count_or_errors_give_none(self):
        self.assertIsNone(run(self.client(lambda r: httpx.Response(200, json={"texts": ["only one"]})).correct(["a", "b"])))
        self.assertIsNone(run(self.client(lambda r: httpx.Response(400, json={"detail": "bad"})).correct(["a"])))


class Compare(unittest.TestCase):
    def test_only_changed_lines_are_listed_with_their_fixes(self):
        from app.spell.compare import compare_lines

        corr = [{"line": 2, "wrong": "thưc", "right": "thực", "by": "dict"}]
        changes = compare_lines("Dòng đúng\nViệc thưc hiện", "Dòng đúng\nViệc thực hiện", corr)
        self.assertEqual(len(changes), 1)
        c = changes[0]
        self.assertEqual((c["line"], c["before"], c["after"], c["marked"]), (2, "Việc thưc hiện", "Việc thực hiện", "Việc [thưc→thực] hiện"))
        self.assertEqual(c["segments"], [{"type": "same", "text": "Việc"}, {"type": "fix", "before": "thưc", "after": "thực", "by": "dict"},
                                         {"type": "same", "text": "hiện"}])
        self.assertEqual(c["fixes"], [{"wrong": "thưc", "right": "thực", "by": "dict"}])

    def test_marking_handles_multi_word_replacements(self):
        from app.spell.compare import mark_changes

        self.assertEqual(mark_changes("uy ban nhan dan tinh", "UBND tinh"), "[uy ban nhan dan→UBND] tinh")


class UnknownWords(unittest.TestCase):
    PAGES = [
        {"page": 1, "text": "Việc thực hiện nghị quyết ngoxbak\nGiấy chứng nhận QSDĐ"},
        {"page": 2, "text": "Hồ sơ ngoxbak gửi Facebook"},
    ]

    def test_reports_words_missing_from_every_dictionary(self):
        words, total = find_unknown_words(self.PAGES, LEX)
        by_word = {w["word"]: w for w in words}
        self.assertEqual(total, 3)
        self.assertEqual(by_word["ngoxbak"]["kind"], "vietnamese")
        self.assertEqual(by_word["ngoxbak"]["count"], 2)
        self.assertEqual(by_word["ngoxbak"]["pages"], [1, 2])
        self.assertIn("ngoxbak", by_word["ngoxbak"]["samples"][0])
        self.assertEqual(by_word["QSDĐ"]["kind"], "acronym")
        self.assertEqual(by_word["Facebook"]["kind"], "foreign")
        self.assertEqual(words[0]["word"], "ngoxbak")  # Từ tiếng Việt đứng đầu

    def test_known_words_and_short_tokens_are_not_reported(self):
        words, _ = find_unknown_words([{"page": 1, "text": "Việc thực hiện nghị quyết a b"}], LEX)
        self.assertEqual(words, [])

    def test_kinds_can_be_filtered(self):
        words, total = find_unknown_words(self.PAGES, LEX, include=("vietnamese",))
        self.assertEqual([w["word"] for w in words], ["ngoxbak"])
        self.assertEqual(total, 1)

    def test_notice_is_in_vietnamese_and_lists_words(self):
        words, total = find_unknown_words(self.PAGES, LEX)
        notice = build_notice(words, total)
        self.assertIn("3 từ không có trong từ điển", notice)
        self.assertIn("ngoxbak", notice)
        self.assertIsNone(build_notice([], 0))

    def test_a_word_known_to_any_loaded_dictionary_is_not_unknown(self):
        tiny = Lexicon(["xin chào"], "only")
        words, _ = find_unknown_words([{"page": 1, "text": "xin chào bạn"}], tiny)
        self.assertEqual([w["word"] for w in words], ["bạn"])

    def test_words_fixed_by_the_spell_checker_are_not_reported(self):
        pages = [{"page": 1, "raw_text": "Việc thưc hiện", "text": "Việc thưc hiện"}]
        run(flow.postprocess(pages, SpellChecker(LEX, None, mode="dict")))
        words, _ = find_unknown_words(pages, LEX)
        self.assertEqual(words, [])


def make_pdf(text="Trang van ban"):
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


class Mocks:
    """Giả lập Chandra và ProtonX."""

    def __init__(self, chandra_html="<p>Xin chào</p>", spellfix=None, chandra_fail=False, speech="Xin chào", speech_status=200):
        self.chandra_html, self.spellfix, self.chandra_fail = chandra_html, spellfix, chandra_fail
        self.speech, self.speech_status = speech, speech_status
        self.chandra_requests, self.spellfix_requests, self.speech_requests = [], [], []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "phowhisper":
            self.speech_requests.append(request.content)
            if self.speech_status != 200:
                return httpx.Response(self.speech_status, json={"error": "down"})
            return httpx.Response(200, json={"text": self.speech})
        body = json.loads(request.content)
        if request.url.host == "chandra":
            self.chandra_requests.append(body)
            if self.chandra_fail:
                return httpx.Response(503, json={"error": "down"})
            # Danh sách: trả lần lượt, hết thì lặp lại câu cuối
            html_out = self.chandra_html
            if isinstance(html_out, list):
                html_out = html_out.pop(0) if len(html_out) > 1 else html_out[0]
            return httpx.Response(200, json={"choices": [{"message": {"content": html_out}, "finish_reason": "stop"}]})
        self.spellfix_requests.append(body)
        texts = self.spellfix(body["texts"]) if self.spellfix else body["texts"]
        return httpx.Response(200, json={"texts": texts})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def make_settings(**overrides) -> Settings:
    return Settings(
        api_username="tester",
        api_password="secret-pass-123",
        chandra_url="http://chandra/v1/chat/completions",
        spellfix_url="http://spellfix/spellfix/correct",
        phowhisper_url="http://phowhisper/v1/audio/transcriptions",
        deskew=False,
        **overrides,
    )


def make_client(mocks: Mocks, **overrides):
    app = create_app(make_settings(**overrides), mocks.client())
    return TestClient(app)


KEY = {"Authorization": "Basic " + base64.b64encode(b"tester:secret-pass-123").decode()}


class TableLayout(unittest.TestCase):
    TABLE = (
        '<div data-label="Section-Header"><h2>BÁO CÁO</h2></div>'
        '<div data-label="Figure"><img alt="logo"/><p>mô tả ảnh</p></div>'
        '<div data-label="Table"><table><caption>Bảng 1</caption>'
        '<tr><th rowspan="2">STT</th><th colspan="2">Kết quả</th></tr>'
        '<tr><th>Đạt</th><th>Không</th></tr>'
        '<tr><td>1</td><td>Người đưoc giao</td><td></td></tr></table></div>'
        '<ul><li>Mục 1<ul><li>Mục 1.1</li></ul></li><li>Mục 2</li></ul>'
    )

    def test_layout_keeps_table_structure(self):
        from app.pipeline.layout import layout_html, layout_text, parse_layout

        blocks = parse_layout(self.TABLE)
        self.assertEqual([b["type"] for b in blocks], ["text", "table", "list"])
        self.assertEqual(layout_text(blocks),
                         "BÁO CÁO\n\nBảng 1\nSTT | Kết quả\nĐạt | Không\n1 | Người đưoc giao | \n\nMục 1\nMục 1.1\nMục 2")
        page = layout_html(blocks)
        self.assertNotIn("mô tả ảnh", page)
        self.assertIn('<th rowspan="2">STT</th><th colspan="2">Kết quả</th>', page)
        self.assertIn("<caption>Bảng 1</caption>", page)
        self.assertIn("<ul><li>Mục 1<ul><li>Mục 1.1</li></ul></li><li>Mục 2</li></ul>", page)

    def test_text_is_escaped_and_bad_text_keeps_original(self):
        from app.pipeline.layout import layout_html, parse_layout

        blocks = parse_layout("<table><tr><td>&lt;script&gt;</td><td>a</td></tr></table>")
        self.assertIn("<td>&lt;script&gt;</td>", layout_html(blocks))
        # Số dòng lệch: giữ chữ gốc
        self.assertIn("<td>a</td>", layout_html(blocks, "x\ny\nz"))
        # Số ô lệch trong một hàng: giữ chữ gốc của hàng đó
        self.assertIn("<td>a</td>", layout_html(blocks, "only one cell"))

    def test_sync_ocr_returns_table_html_with_spelling_fixed(self):
        mocks = Mocks(self.TABLE, swap("đưoc", "được"))
        with make_client(mocks) as client:
            r = client.post("/api/ocr-fullpage/sync", headers=KEY, files={"file": ("a.png", make_png(), "image/png")})
        self.assertEqual(r.status_code, 200, r.text)
        page = r.json()["pages"][0]
        self.assertNotIn("layout", page)
        self.assertIn("1 | Người được giao |", page["text"])
        self.assertIn('<tr><td>1</td><td>Người được giao</td><td></td></tr>', page["html"])
        self.assertIn('<th colspan="2">Kết quả</th>', page["html"])

    def test_text_file_pages_get_paragraph_html(self):
        from app.pipeline.layout import text_html

        self.assertEqual(text_html("Dòng 1\nDòng <2>\n\nĐoạn 2"), "<p>Dòng 1<br>Dòng &lt;2&gt;</p>\n<p>Đoạn 2</p>")


def make_table_docx() -> bytes:
    """Word có tiêu đề, danh sách và bảng gộp ô (2 hàng tiêu đề, cột "Kết quả" gộp ngang, ô gộp dọc)."""
    import io

    from docx import Document

    d = Document()
    d.add_heading("BÁO CÁO KẾT QUẢ", level=1)
    d.add_paragraph("Người đưoc giao")
    d.add_paragraph("Mục một", style="List Bullet")
    t = d.add_table(rows=4, cols=5)
    for i, h in enumerate(["STT", "Đơn vị", "Kết quả", "", "Ghi chú"]):
        t.cell(0, i).text = h
    for i, h in enumerate(["", "", "Đạt", "Không đạt", ""]):
        t.cell(1, i).text = h
    t.cell(0, 2).merge(t.cell(0, 3))
    for c in (0, 1, 4):
        t.cell(0, c).merge(t.cell(1, c))
    for i, v in enumerate(["1", "Phòng Tài chính", "12", "1", ""]):
        t.cell(2, i).text = v
    for i, v in enumerate(["2", "Phòng Nội vụ", "Chưa báo cáo", "", "Bổ sung"]):
        t.cell(3, i).text = v
    t.cell(3, 2).merge(t.cell(3, 3))
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


class WordTables(unittest.TestCase):
    def test_docx_layout_keeps_merged_cells(self):
        from app.inputs.textfile import read_text_pages
        from app.pipeline.layout import layout_html

        path = Path(os.environ["TEMP_DIR"]) / "bang.docx"
        path.write_bytes(make_table_docx())
        pages, problem = read_text_pages(path)
        self.assertEqual(problem, "")
        self.assertEqual(len(pages), 1)
        self.assertIn("STT | Đơn vị | Kết quả | Ghi chú\nĐạt | Không đạt\n1 | Phòng Tài chính | 12 | 1 | ", pages[0]["text"])
        page = layout_html(pages[0]["layout"])
        self.assertIn("<h1>BÁO CÁO KẾT QUẢ</h1>", page)
        self.assertIn("<ul><li>Mục một</li></ul>", page)
        self.assertIn('<td rowspan="2">STT</td><td rowspan="2">Đơn vị</td><td colspan="2">Kết quả</td><td rowspan="2">Ghi chú</td>', page)
        self.assertIn('<td colspan="2">Chưa báo cáo</td><td>Bổ sung</td>', page)

    def test_docx_layout_converts_legacy_fonts(self):
        from app.inputs.textfile import read_text_pages

        path = Path(os.environ["TEMP_DIR"]) / "vni-layout.docx"
        path.write_bytes(make_docx(["Vieäc thöïc hieän"], ["Ñaûng boä tænh"]))
        pages, problem = read_text_pages(path)
        self.assertEqual(([p["text"] for p in pages], problem), (["Việc thực hiện", "Đảng bộ tỉnh"], ""))

    def test_sync_docx_upload_returns_table_html(self):
        mocks = Mocks(spellfix=swap("đưoc", "được"))
        with make_client(mocks, spell_text_files=True) as client:
            r = client.post("/api/ocr-fullpage/sync", headers=KEY, files={"file": ("bang.docx", make_table_docx(), "application/octet-stream")})
        self.assertEqual(r.status_code, 200, r.text)
        page = r.json()["pages"][0]
        self.assertEqual(page["source"], "text_file")
        self.assertNotIn("layout", page)
        self.assertEqual(mocks.chandra_requests, [])
        self.assertIn("<p>Người được giao</p>", page["html"])
        self.assertIn('<td colspan="2">Kết quả</td>', page["html"])

    def test_worker_text_page_keeps_layout(self):
        from app.inputs.collect import text_page

        layout = [{"type": "text", "tag": "p", "lines": ["A"]}]
        self.assertEqual(text_page(3, {"text": "A", "layout": layout})["layout"], layout)
        self.assertNotIn("layout", text_page(1, {"text": "A", "layout": None}))


class SyncApi(unittest.TestCase):
    def test_pipeline_ocr_protonx_and_unknown_word_notice(self):
        mocks = Mocks("<p>Người đưoc giao đất qwxzv</p>", swap("đưoc", "được"))
        with make_client(mocks) as client:
            r = client.post("/api/ocr-fullpage/sync", headers=KEY, files={"file": ("a.pdf", make_pdf(), "application/pdf")}, data={"include_raw": "true"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["text"], "Người được giao đất qwxzv")
        self.assertEqual(body["pages"][0]["raw_text"], "Người đưoc giao đất qwxzv")
        self.assertEqual(body["pages"][0]["corrections"][0]["by"], "protonx")
        self.assertEqual(body["correction_total"], 1)
        self.assertEqual(body["changes"], [{
            "page": 1, "line": 1, "before": "Người đưoc giao đất qwxzv", "after": "Người được giao đất qwxzv",
            "marked": "Người [đưoc→được] giao đất qwxzv",
            "segments": [{"type": "same", "text": "Người"}, {"type": "fix", "before": "đưoc", "after": "được", "by": "protonx"},
                         {"type": "same", "text": "giao đất qwxzv"}],
            "fixes": [{"wrong": "đưoc", "right": "được", "by": "protonx"}],
        }])
        self.assertNotIn("changes", body["pages"][0])
        self.assertEqual([w["word"] for w in body["unknown_words"]], ["qwxzv"])
        self.assertIn("qwxzv", body["notice"])
        sent = mocks.chandra_requests[0]
        self.assertEqual(sent["messages"][0]["content"][0]["type"], "image_url")
        self.assertEqual(sent["messages"][0]["content"][1], {"type": "text", "text": OCR_LAYOUT_PROMPT})
        self.assertEqual(sent["temperature"], 0)

    def test_no_notice_when_every_word_is_known(self):
        with make_client(Mocks("<p>Việc thực hiện nghị quyết</p>")) as client:
            r = client.post("/api/ocr-fullpage/sync", headers=KEY, files={"file": ("a.pdf", make_pdf(), "application/pdf")})
        self.assertIsNone(r.json()["notice"])
        self.assertEqual(r.json()["unknown_words"], [])

    def test_unknown_report_can_be_disabled(self):
        with make_client(Mocks("<p>qwxzv</p>"), unknown_report=False) as client:
            r = client.post("/api/ocr-fullpage/sync", headers=KEY, files={"file": ("a.pdf", make_pdf(), "application/pdf")})
        self.assertIsNone(r.json()["notice"])

    def test_auth(self):
        with make_client(Mocks()) as client:
            files = {"file": ("a.pdf", make_pdf(), "application/pdf")}
            self.assertEqual(client.post("/api/ocr-fullpage/sync", files=files).status_code, 401)
            denied = client.post("/api/ocr-fullpage/sync", files=files)
            self.assertTrue(denied.headers["www-authenticate"].startswith("Basic"))
            for user, pw in (("tester", "wrong"), ("other", "secret-pass-123"), ("", "")):
                bad = {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}
                self.assertEqual(client.post("/api/ocr-fullpage/sync", headers=bad, files=files).status_code, 401)
            self.assertEqual(client.post("/api/ocr-fullpage/sync", headers={"X-API-Key": "secret-pass-123"}, files=files).status_code, 401)
            ok = client.post("/api/ocr-fullpage/sync", auth=("tester", "secret-pass-123"), files=files)
            self.assertEqual(ok.status_code, 200)
            self.assertEqual(client.get("/health").json(), {"status": "ok"})
            self.assertEqual(client.get("/docs").status_code, 404)

    def test_chandra_down_falls_back_to_pdf_text_layer(self):
        with make_client(Mocks(chandra_fail=True), spell_mode="off") as client:
            r = client.post("/api/ocr-fullpage/sync", headers=KEY, files={"file": ("a.pdf", make_pdf("Noi dung co san"), "application/pdf")})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["pages"][0]["source"], "pdf_text")

    def test_chandra_down_without_text_layer_is_502_and_hides_upstream(self):
        with make_client(Mocks(chandra_fail=True), spell_mode="off") as client:
            r = client.post("/api/ocr-fullpage/sync", headers=KEY, files={"file": ("a.pdf", make_pdf(""), "application/pdf")})
        self.assertEqual(r.status_code, 502)
        self.assertNotIn("chandra", r.text.lower())

    def test_bad_inputs(self):
        with make_client(Mocks(), max_pages=1, max_upload_mb=1) as client:
            junk = client.post("/api/ocr-fullpage/sync", headers=KEY, files={"file": ("a.bin", b"not a document", "application/octet-stream")})
            self.assertEqual(junk.status_code, 415)
            two = fitz.open()
            two.new_page()
            two.new_page()
            many = client.post("/api/ocr-fullpage/sync", headers=KEY, files={"file": ("a.pdf", two.tobytes(), "application/pdf")})
            self.assertEqual(many.status_code, 413)
            big = client.post("/api/ocr-fullpage/sync", headers=KEY, files={"file": ("a.pdf", b"%PDF-" + b"0" * (2 * 1024 * 1024), "application/pdf")})
            self.assertEqual(big.status_code, 413)

    def test_spellcheck_endpoint_returns_report(self):
        with make_client(Mocks()) as client:
            r = client.post("/api/spellcheck", headers=KEY, json={"text": "Việc thưc hiện nghị quyêt qwxzv"})
        body = r.json()
        self.assertEqual(body["text"], "Việc thực hiện nghị quyết qwxzv")
        self.assertEqual([w["word"] for w in body["unknown_words"]], ["qwxzv"])

    def test_image_upload(self):
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (200, 100), "white").save(buf, format="PNG")
        with make_client(Mocks("<p>Ảnh</p>"), spell_mode="off") as client:
            r = client.post("/api/ocr-fullpage/sync", headers=KEY, files={"file": ("a.png", buf.getvalue(), "image/png")})
        self.assertEqual(r.json()["text"], "Ảnh")


class AsyncApi(unittest.TestCase):
    BODY = {
        "s3_path": "s3://bucket/dir/a.pdf",
        "access_key": "AK-SECRET-VALUE",
        "secret_key": "SK-SECRET-VALUE",
        "endpoint": "http://minio:9000",
        "callback_url": "http://hooks.example/cb",
        "document_id": 42,
    }

    def test_submit_queues_the_chain_with_a_request_id(self):
        with make_client(Mocks()) as client, mock.patch("app.api.main.submit_fullpage") as submit:
            r = client.post("/api/ocr-fullpage", headers=KEY, json=self.BODY)
        self.assertEqual(r.status_code, 202, r.text)
        rid = r.json()["request_id"]
        args = submit.call_args.args
        self.assertEqual(args[0], rid)
        self.assertEqual(args[1:4], ("s3://bucket/dir/a.pdf", "AK-SECRET-VALUE", "SK-SECRET-VALUE"))
        self.assertEqual(args[6:], (42, True))

    def test_validation_errors_never_echo_credentials(self):
        bad = dict(self.BODY, s3_path="not-an-s3-path")
        with make_client(Mocks()) as client:
            r = client.post("/api/ocr-fullpage", headers=KEY, json=bad)
            bad_endpoint = client.post("/api/ocr-fullpage", headers=KEY, json=dict(self.BODY, endpoint="ftp://minio"))
        for resp in (r, bad_endpoint):
            self.assertEqual(resp.status_code, 422)
            self.assertNotIn("SECRET-VALUE", resp.text)

    def test_missing_s3_settings_are_rejected(self):
        body = {k: v for k, v in self.BODY.items() if k != "endpoint"}
        with make_client(Mocks()) as client, mock.patch("app.api.main.submit_fullpage") as submit:
            r = client.post("/api/ocr-fullpage", headers=KEY, json=body)
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("SECRET-VALUE", r.text)
        submit.assert_not_called()

    def test_s3_settings_come_from_env_and_stay_out_of_the_queue(self):
        body = {"s3_path": "s3://bucket/dir/a.pdf"}
        env_s3 = dict(s3_endpoint="http://minio:9000", s3_access_key="ENV-AK", s3_secret_key="ENV-SK")
        with make_client(Mocks(), **env_s3) as client, mock.patch("app.api.main.submit_fullpage") as submit:
            r = client.post("/api/ocr-fullpage", headers=KEY, json=body)
        self.assertEqual(r.status_code, 202, r.text)
        self.assertEqual(submit.call_args.args[2:5], ("", "", ""))
        self.assertNotIn("ENV-", repr(submit.call_args))

    def test_callback_host_allow_list(self):
        with make_client(Mocks(), webhook_allowed_hosts=("trusted.example",)) as client, mock.patch("app.api.main.submit_fullpage"):
            r = client.post("/api/ocr-fullpage", headers=KEY, json=self.BODY)
        self.assertEqual(r.status_code, 400)

    def test_request_requires_key(self):
        with make_client(Mocks()) as client:
            self.assertEqual(client.post("/api/ocr-fullpage", json=self.BODY).status_code, 401)
            self.assertEqual(client.get("/api/request-status/abcdefgh12345678").status_code, 401)
            self.assertEqual(client.get("/api/workers/status").status_code, 401)

    def test_request_model_hides_secrets_in_repr(self):
        text = repr(FullPageRequest(**self.BODY))
        self.assertNotIn("SECRET-VALUE", text)

    def test_worker_endpoints_validate_queue_names(self):
        with make_client(Mocks()) as client:
            bad = client.post("/api/workers/start", headers=KEY, json={"queue_name": "evil; rm -rf /", "num_workers": 1})
            self.assertEqual(bad.status_code, 422)
            presets = client.get("/api/workers/presets", headers=KEY).json()["presets"]
        self.assertEqual(set(presets), set(QUEUES))


class Pipeline(unittest.TestCase):
    """Chạy 4 task Celery liên tiếp với mạng giả."""

    ENV = {
        "CHANDRA_URL": "http://chandra/v1/chat/completions",
        "SPELLFIX_API_URL": "http://spellfix/spellfix/correct",
        "PHOWHISPER_URL": "http://phowhisper/v1/audio/transcriptions",
        "DESKEW": "false",
        "SPELL_MODE": "both",
    }

    def setUp(self):
        celery_app.conf.task_always_eager = True
        celery_app.conf.task_eager_propagates = True
        self.hooks, self.downloads = [], []
        self.env = mock.patch.dict(os.environ, self.ENV)
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_chain(self, mocks, download=None, request_id="req-0000-aaaa", callback="http://hooks.example/cb", ak="AK", sk="SK",
                  endpoint="http://minio:9000", files=None, s3_path="s3://bucket/a.pdf"):
        test = self
        files = files or {"a.pdf": make_pdf("Noi dung co san")}

        class Storage(FakeStorage):
            def __init__(self, access_key, secret_key, endpoint, region, ssl=False):
                super().__init__(files)
                test.downloads.append((access_key, secret_key, endpoint, ssl))
                test.storage = self

        if download:
            Storage.download = lambda self, *a: download()
        with mock.patch.object(tasks, "S3Storage", Storage), \
             mock.patch.object(flow, "make_http_client", mocks.client), \
             mock.patch.object(tasks, "send_webhook", lambda url, payload, *a: self.hooks.append((url, payload)) or True):
            return tasks.submit_fullpage(request_id, s3_path, ak, sk, endpoint, callback, 7, True)

    def test_full_pipeline_result_webhook_and_cleanup(self):
        mocks = Mocks("<p>Người đưoc giao đất qwxzv</p>", swap("đưoc", "được"))
        result = self.run_chain(mocks).get()
        self.assertEqual(result["text"], "Người được giao đất qwxzv")
        self.assertEqual(result["document_id"], 7)
        self.assertFalse(result["saved_to_db"])
        self.assertEqual([w["word"] for w in result["unknown_words"]], ["qwxzv"])
        self.assertIn("qwxzv", result["notice"])
        self.assertNotIn("raw_text", result["pages"][0])
        (url, payload), = self.hooks
        self.assertEqual(url, "http://hooks.example/cb")
        self.assertEqual(payload["meta"]["error_code"], 200)
        self.assertEqual(payload["data"]["unknown_words"][0]["word"], "qwxzv")
        self.assertEqual(payload["data"]["document_id"], 7)
        self.assertFalse((Path(os.environ["TEMP_DIR"]) / "req-0000-aaaa").exists())
        self.assertNotIn("AK", json.dumps(payload))

    def test_worker_falls_back_to_s3_settings_from_env(self):
        env = {"S3_ENDPOINT": "minio:9000", "S3_ACCESS_KEY": "ENV-AK", "S3_SECRET_KEY": "ENV-SK", "S3_SSL": "true"}
        with mock.patch.dict(os.environ, env):
            self.run_chain(Mocks(), request_id="req-0000-eeee", ak="", sk="", endpoint="").get()
            self.run_chain(Mocks(), request_id="req-0000-ffff").get()
        self.assertEqual(self.downloads, [("ENV-AK", "ENV-SK", "minio:9000", True), ("AK", "SK", "http://minio:9000", True)])

    def test_chandra_down_uses_pdf_text_layer(self):
        result = self.run_chain(Mocks(chandra_fail=True), request_id="req-0000-bbbb").get()
        self.assertEqual(result["pages"][0]["source"], "pdf_text")
        self.assertEqual(result["text"], "Noi dung co san")

    def test_download_failure_sends_a_generic_error_webhook_and_cleans_up(self):
        def boom(*a, **k):
            os.makedirs(os.path.join(os.environ["TEMP_DIR"], "req-0000-cccc"), exist_ok=True)
            raise RuntimeError("connect to http://minio:9000 with AK / SK failed")

        with self.assertRaises(RuntimeError):
            self.run_chain(Mocks(), download=boom, request_id="req-0000-cccc")
        (url, payload), = self.hooks
        self.assertEqual(payload["meta"]["error_code"], 500)
        self.assertEqual(payload["meta"]["error_message"], "Processing failed")
        self.assertNotIn("minio", json.dumps(payload))
        self.assertFalse((Path(os.environ["TEMP_DIR"]) / "req-0000-cccc").exists())

    def test_no_callback_means_no_webhook(self):
        self.run_chain(Mocks(), request_id="req-0000-dddd", callback="").get()
        self.assertEqual(self.hooks, [])

    def test_word_file_next_to_the_pdf_is_used_instead_of_ocr(self):
        mocks = Mocks("<p>không được dùng</p>")
        files = {"a.pdf": make_pdf(), "a.docx": make_docx(["Trang một"], ["Trang hai"]), "khac.pdf": make_pdf()}
        result = self.run_chain(mocks, request_id="req-0000-gggg", files=files).get()
        self.assertEqual(result["input"], {"mode": "text", "files": ["a.docx"], "skipped": []})
        self.assertEqual([p["text"] for p in result["pages"]], ["Trang một", "Trang hai"])
        self.assertEqual({p["source"] for p in result["pages"]}, {"text_file"})
        self.assertEqual((mocks.chandra_requests, mocks.spellfix_requests), ([], []))
        self.assertEqual(self.storage.fetched, ["a.docx"])

    def test_unreadable_text_file_falls_back_to_ocr(self):
        mocks = Mocks("<p>Xin chào</p>")
        files = {"a.pdf": make_pdf(), "a.txt": "§§§ 123 ### 456 !!!".encode()}
        result = self.run_chain(mocks, request_id="req-0000-hhhh", files=files).get()
        self.assertEqual(result["input"]["mode"], "ocr")
        self.assertEqual(result["input"]["files"], ["a.pdf"])
        self.assertEqual(result["input"]["skipped"][0]["file"], "a.txt")
        self.assertEqual(result["text"], "Xin chào")

    def test_digital_pdf_text_is_used_without_ocr(self):
        mocks = Mocks("<p>không được dùng</p>")
        result = self.run_chain(mocks, request_id="req-0000-jjjj", files={"a.pdf": make_pdf_pages(GOOD, GOOD)}).get()
        self.assertEqual([p["source"] for p in result["pages"]], ["pdf_text", "pdf_text"])
        self.assertEqual(result["input"]["pdf_text"], [{"file": "a.pdf", "text_pages": 2, "ocr_pages": 0}])
        self.assertEqual((mocks.chandra_requests, mocks.spellfix_requests), ([], []))
        self.assertFalse(list((Path(os.environ["TEMP_DIR"])).glob("req-0000-jjjj/pages/*.jpg")))

    def test_folder_of_images_is_ocred_page_by_page_in_order(self):
        files = {f"trang{n}.png": make_png() for n in (10, 2, 1)}
        files["ghi-chu.csv"] = b"x"
        result = self.run_chain(Mocks("<p>Ảnh</p>"), request_id="req-0000-iiii", files=files, s3_path="s3://bucket/sach/").get()
        self.assertEqual(result["input"]["files"], ["trang1.png", "trang2.png", "trang10.png"])
        self.assertEqual([p["page"] for p in result["pages"]], [1, 2, 3])


class FakeStorage:
    """S3Storage giả: files là {tên: bytes} trong một thư mục."""

    def __init__(self, files):
        self.files, self.fetched = files, []

    def candidates(self, s3_path, siblings=True):
        from app.storage.s3 import parse_s3_path

        bucket, key = parse_s3_path(s3_path)
        folder = key if key.endswith("/") else key.rsplit("/", 1)[0] + "/" if "/" in key else ""
        keys = [folder + n for n in self.files]
        if key.endswith("/"):
            return bucket, keys, ""
        stem = Path(key).stem.lower()
        return bucket, [key] + [k for k in keys if k != key and Path(k).stem.lower() == stem], key

    def download(self, bucket, key, dest, max_bytes):
        name = key.rsplit("/", 1)[-1]
        self.fetched.append(name)
        Path(dest).mkdir(parents=True, exist_ok=True)
        target = Path(dest) / name
        target.write_bytes(self.files[name])
        return target


def make_docx(*pages):
    """docx có ngắt trang giữa các trang."""
    import io

    from docx import Document
    from docx.enum.text import WD_BREAK

    doc = Document()
    for i, lines in enumerate(pages):
        for j, line in enumerate(lines):
            para = doc.add_paragraph(line)
            if i < len(pages) - 1 and j == len(lines) - 1:
                para.add_run().add_break(WD_BREAK.PAGE)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def make_png():
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (120, 80), "white").save(buf, format="PNG")
    return buf.getvalue()


class InputFiles(unittest.TestCase):
    """Chọn và đọc file đầu vào."""

    def test_plan_prefers_text_files_and_pdfs_over_images(self):
        from app.inputs.select import plan

        texts, ocr = plan(["d/b.txt", "d/a.pdf", "d/a.docx", "d/~$a.docx", "d/x.jpg", "d/a.doc", "d/z.zip"])
        self.assertEqual(texts, ["d/a.docx", "d/a.doc", "d/b.txt"])
        self.assertEqual(ocr, ["d/a.pdf"])
        self.assertEqual(plan(["d/p10.jpg", "d/p9.jpg"]), ([], ["d/p9.jpg", "d/p10.jpg"]))
        # File được chỉ định đứng đầu, chỉ OCR đúng file đó
        texts, ocr = plan(["d/x.jpg", "d/x.txt", "d/a.docx"], requested="d/x.jpg")
        self.assertEqual((texts[0], ocr), ("d/x.txt", ["d/x.jpg"]))

    def test_docx_pages_follow_word_page_breaks(self):
        from app.inputs.textfile import read_text_file

        path = Path(os.environ["TEMP_DIR"]) / "t.docx"
        path.write_bytes(make_docx(["Dòng 1", "Dòng 2"], ["Trang sau"]))
        self.assertEqual(read_text_file(path), (["Dòng 1\nDòng 2", "Trang sau"], ""))

    def test_txt_encodings_and_tcvn3_are_converted(self):
        from app.inputs.textfile import read_text_file

        path = Path(os.environ["TEMP_DIR"]) / "t.txt"
        for data in ("Việc thực hiện".encode("utf-8-sig"), "Việc thực hiện".encode("utf-16"), "ViÖc thùc hiÖn".encode("latin-1")):
            path.write_bytes(data)
            self.assertEqual(read_text_file(path), (["Việc thực hiện"], ""))

    def test_vni_text_is_converted_and_unicode_left_alone(self):
        from app.inputs.textfile import fix_legacy, quality_problem, read_text_file

        # VNI-Windows
        self.assertEqual(fix_legacy("HÖÔÙNG DAÃN thöïc hieän nghò quyeát"), "HƯỚNG DẪN thực hiện nghị quyết")
        path = Path(os.environ["TEMP_DIR"]) / "vni.docx"
        path.write_bytes(make_docx(["Vieäc thöïc hieän"], ["Ñaûng boä tænh"]))
        self.assertEqual(read_text_file(path), (["Việc thực hiện", "Đảng bộ tỉnh"], ""))
        # Unicode có á, ò... không bị đổi nhầm
        for text in ("bà má có lá cờ", "Việc thực hiện nghị quyết", "Report of the board"):
            self.assertEqual(fix_legacy(text), text)
        # Mã khác không đổi được thì báo lỗi, không dùng
        self.assertIn("phông cũ", quality_problem(["Ìþ ßÿ Ýþð ÐÞ Ïþü ÞÐ Ìÿ ÞÿÐ"]))
        self.assertEqual(quality_problem(["Việc thực hiện nghị quyết"]), "")

    def test_long_text_without_page_breaks_is_split_on_lines(self):
        from app.inputs.textfile import read_text_file

        path = Path(os.environ["TEMP_DIR"]) / "long.txt"
        path.write_text("\n".join(f"Dòng số {i}" for i in range(400)), encoding="utf-8")
        pages, problem = read_text_file(path, page_chars=500)
        self.assertEqual(problem, "")
        self.assertGreater(len(pages), 5)
        self.assertTrue(all(len(p) <= 500 for p in pages))
        self.assertEqual("\n".join(pages).split("\n")[-1], "Dòng số 399")

    def test_s3_candidates_include_same_name_files_and_folders(self):
        from app.storage.s3 import S3Storage

        storage = S3Storage.__new__(S3Storage)
        storage.client = mock.Mock()
        storage.client.list_objects_v2.return_value = {
            "Contents": [{"Key": "d/"}, {"Key": "d/a.pdf"}, {"Key": "d/A.docx"}, {"Key": "d/b.txt"}], "IsTruncated": False,
        }
        self.assertEqual(storage.candidates("s3://bk/d/a.pdf"), ("bk", ["d/a.pdf", "d/A.docx"], "d/a.pdf"))
        self.assertEqual(storage.candidates("s3://bk/d/"), ("bk", ["d/a.pdf", "d/A.docx", "d/b.txt"], ""))
        self.assertEqual(storage.client.list_objects_v2.call_args.kwargs["Delimiter"], "/")
        storage.client.list_objects_v2.side_effect = RuntimeError("AccessDenied")
        self.assertEqual(storage.candidates("s3://bk/d/a.pdf"), ("bk", ["d/a.pdf"], "d/a.pdf"))

    def test_sync_upload_of_a_text_file_skips_ocr_and_spelling(self):
        mocks = Mocks()
        with make_client(mocks) as client:
            r = client.post("/api/ocr-fullpage/sync", headers=KEY, files={"file": ("ghi chu.txt", "Việc thưc hiện".encode(), "text/plain")})
        body = r.json()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((body["text"], body["correction_total"]), ("Việc thưc hiện", 0))
        self.assertEqual(body["input"], {"mode": "text", "files": ["ghi chu.txt"], "skipped": []})
        self.assertEqual((mocks.chandra_requests, mocks.spellfix_requests), ([], []))

    def test_text_files_can_be_spellchecked_when_enabled(self):
        with make_client(Mocks(), spell_text_files=True, spell_mode="dict") as client:
            r = client.post("/api/ocr-fullpage/sync", headers=KEY, files={"file": ("a.txt", "Việc thưc hiện".encode(), "text/plain")})
        self.assertEqual(r.json()["text"], "Việc thực hiện")



# Câu chỉ gồm âm tiết không dấu có trong từ điển (font PDF chuẩn không có chữ Việt)
GOOD = "ba co ban van con sau hai mua tay an anh em nam tin thu ta la mai ca cho"
TCVN3_LINE = "ViÖc thùc hiÖn nghÞ quyÕt cña Héi ®ång nh©n d©n"


def make_pdf_pages(*texts):
    doc = fitz.open()
    for text in texts:
        page = doc.new_page()
        for i, line in enumerate(text.split("\n")):
            page.insert_text((72, 72 + 16 * i), line)
    data = doc.tobytes()
    doc.close()
    return data


class PdfText(unittest.TestCase):
    """Lớp text PDF: dùng trực tiếp; một trang lỗi Unicode là OCR cả file."""

    def test_unicode_problems_are_detected(self):
        from app.inputs.pdftext import unicode_problem

        self.assertEqual(unicode_problem("Việc thực hiện nghị quyết của Hội đồng"), "")
        cases = {
            "Vi\ufffdt Nam": "thay thế",
            "(cid:68)(cid:72)(cid:81)": "cid",
            "Vi\uf0a7t Nam": "vùng riêng",
            "Vi\x07t Nam": "điều khiển",
            TCVN3_LINE: "TCVN3",
            "Coäng hoøa xaõ hoäi chuû nghóa Vieät Nam": "VNI",
            "liên quan đê \u0301n chiến tranh": "tách rời",
            "Vie^.t Nam Vi ´ê t": "tách rời",
        }
        for text, needle in cases.items():
            self.assertIn(needle, unicode_problem(text), text)

    def test_one_bad_page_rejects_the_whole_file(self):
        from app.inputs.pdftext import file_problem

        self.assertEqual(file_problem([GOOD, "", GOOD], LEX, 0.9), "")
        self.assertTrue(file_problem([GOOD, TCVN3_LINE, GOOD], LEX, 0.9).startswith("trang 2:"))

    def test_low_dictionary_rate_over_the_file_rejects_it(self):
        from app.inputs.pdftext import file_problem

        garbage = " ".join(["Thu vien Quoc gia Viet Nam xqzt brrk"] * 10)
        self.assertIn("từ điển", file_problem([garbage], LEX, 0.9))
        # Ít từ quá thì không xét tỉ lệ
        self.assertEqual(file_problem(["Quoc xqzt"], LEX, 0.9), "")

    def test_good_pages_skip_ocr_and_empty_pages_are_ocred(self):
        mocks = Mocks("<p>Trang scan</p>", swap("x", "y"))
        with make_client(mocks) as client:
            r = client.post("/api/ocr-fullpage/sync", headers=KEY, files={"file": ("a.pdf", make_pdf_pages(GOOD, ""), "application/pdf")})
        body = r.json()
        self.assertEqual([p["source"] for p in body["pages"]], ["pdf_text", "chandra"])
        self.assertEqual(body["pages"][0]["text"], GOOD)
        self.assertEqual(len(mocks.chandra_requests), 1)
        # Text PDF là chữ gõ máy: chỉ trang OCR được gửi ProtonX
        self.assertEqual(mocks.spellfix_requests, [{"texts": ["Trang scan"]}])
        self.assertEqual(body["input"]["pdf_text"], [{"file": "a.pdf", "text_pages": 1, "ocr_pages": 1}])

    def test_unicode_error_on_one_page_sends_every_page_to_ocr(self):
        mocks = Mocks("<p>Trang OCR</p>")
        with make_client(mocks, spell_mode="off") as client:
            r = client.post("/api/ocr-fullpage/sync", headers=KEY,
                            files={"file": ("a.pdf", make_pdf_pages(GOOD, TCVN3_LINE, GOOD), "application/pdf")})
        body = r.json()
        self.assertEqual({p["source"] for p in body["pages"]}, {"chandra"})
        self.assertEqual(len(mocks.chandra_requests), 3)
        report = body["input"]["pdf_text"][0]
        self.assertEqual((report["text_pages"], report["ocr_pages"]), (0, 3))
        self.assertIn("trang 2", report["reason"])
        self.assertIn("TCVN3", report["reason"])

    def test_bad_text_layer_is_not_used_when_chandra_is_down(self):
        with make_client(Mocks(chandra_fail=True), spell_mode="off") as client:
            r = client.post("/api/ocr-fullpage/sync", headers=KEY, files={"file": ("a.pdf", make_pdf_pages(TCVN3_LINE), "application/pdf")})
        self.assertEqual(r.status_code, 502)

    def test_can_be_turned_off(self):
        mocks = Mocks()
        with make_client(mocks, pdf_text_first=False, spell_mode="off") as client:
            r = client.post("/api/ocr-fullpage/sync", headers=KEY, files={"file": ("a.pdf", make_pdf_pages(GOOD), "application/pdf")})
        self.assertEqual(r.json()["pages"][0]["source"], "chandra")
        self.assertEqual(r.json()["input"]["pdf_text"][0]["reason"], "PDF_TEXT_FIRST=false")


class FakeCursor:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.log.append((" ".join(sql.split()), params))


class FakeConn:
    def __init__(self):
        self.log, self.closed = [], False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return FakeCursor(self.log)

    def close(self):
        self.closed = True


class Database(unittest.TestCase):
    RESULT = {
        "page_count": 1, "text": "x", "pages": [{"page": 1}], "unknown_total": 1, "notice": "n",
        "unknown_words": [{"word": "qwxzv", "kind": "vietnamese", "count": 2, "pages": [1], "samples": ["ctx"]}],
        "changes": [{"page": 1, "line": 2, "before": "Người đưoc", "after": "Người được", "marked": "Người [đưoc→được]",
                     "fixes": [{"wrong": "đưoc", "right": "được", "by": "protonx"}]}],
    }

    def test_skipped_without_configuration(self):
        self.assertFalse(db.save_result(Settings(), "rid", "s3://b/k", 1, self.RESULT))

    def test_document_and_unknown_words_are_stored(self):
        conn = FakeConn()
        settings = Settings(pg_host="h", pg_db="d", pg_user="u", pg_password="p")
        with mock.patch("psycopg2.connect", return_value=conn) as connect:
            self.assertTrue(db.save_result(settings, "rid", "s3://b/k", 1, self.RESULT))
        self.assertEqual(connect.call_args.kwargs["password"], "p")
        statements = [sql for sql, _ in conn.log]
        self.assertTrue(any(s.startswith("INSERT INTO fullpage_documents") for s in statements))
        words = [params for sql, params in conn.log if sql.startswith("INSERT INTO fullpage_unknown_words")]
        self.assertEqual(words[0][:2], ("rid", "qwxzv"))
        fixes = [params for sql, params in conn.log if sql.startswith("INSERT INTO fullpage_corrections")]
        self.assertEqual(fixes, [("rid", 1, 2, "đưoc", "được", "protonx", "Người đưoc", "Người được")])
        self.assertTrue(conn.closed)


class ScriptedCursor(FakeCursor):
    """Cursor giả trả dữ liệu theo câu SQL."""

    def __init__(self, log, answers):
        super().__init__(log)
        self.answers, self.last = answers, ""

    def execute(self, sql, params=None):
        super().execute(sql, params)
        self.last = " ".join(sql.split())

    def _answer(self):
        return next((rows for prefix, rows in self.answers if self.last.startswith(prefix)), [])

    def fetchone(self):
        rows = self._answer()
        return rows[0] if rows else None

    def fetchall(self):
        return self._answer()


class ScriptedConn(FakeConn):
    def __init__(self, answers):
        super().__init__()
        self.answers = answers

    def cursor(self):
        return ScriptedCursor(self.log, self.answers)


class DocumentsApi(unittest.TestCase):
    """API cho web: đọc tài liệu đã lưu từ DB (giả)."""

    CREATED = __import__("datetime").datetime(2026, 11, 3, 9, 30)
    SUMMARY = ("rid-12345678", "42", "s3://b/a.pdf", 2, 1, 1, "Phát hiện 1 từ", {"mode": "ocr", "files": ["a.pdf"], "skipped": []}, CREATED)
    PAGES = [
        {"page": 1, "text": "Dòng đầu\nNgười được giao đất", "source": "chandra", "error": None,
         "corrections": [{"line": 2, "wrong": "đưoc", "right": "được", "by": "protonx"}]},
        {"page": 2, "text": "Trang hai qwxzv", "source": "chandra", "error": None, "corrections": []},
    ]
    CHANGES = [{"page": 1, "line": 2, "before": "Người đưoc giao đất", "after": "Người được giao đất",
                "marked": "Người [đưoc→được] giao đất",
                "segments": [{"type": "same", "text": "Người"}, {"type": "fix", "before": "đưoc", "after": "được", "by": "protonx"},
                             {"type": "same", "text": "giao đất"}],
                "fixes": [{"wrong": "đưoc", "right": "được", "by": "protonx"}]}]
    UNKNOWN = [("qwxzv", "vietnamese", 1, [2], "Trang hai qwxzv")]

    def client(self, answers):
        conn = ScriptedConn(answers)
        patch = mock.patch("psycopg2.connect", return_value=conn)
        patch.start()
        self.addCleanup(patch.stop)
        return make_client(Mocks(), pg_host="h", pg_db="d", pg_user="u")

    def doc_answers(self):
        return [("SELECT request_id", [self.SUMMARY + (self.PAGES, self.CHANGES)]), ("SELECT word", self.UNKNOWN)]

    def test_list_is_paged_and_filtered(self):
        with self.client([("SELECT count(*)", [(1,)]), ("SELECT request_id", [self.SUMMARY])]) as c:
            r = c.get("/api/documents?limit=10&document_id=42", headers=KEY)
        body = r.json()
        self.assertEqual((body["total"], body["limit"], body["offset"]), (1, 10, 0))
        self.assertEqual(body["items"][0]["request_id"], "rid-12345678")
        self.assertEqual(body["items"][0]["correction_count"], 1)
        self.assertEqual(body["items"][0]["created_at"], "2026-11-03T09:30:00")
        self.assertEqual(body["items"][0]["input"]["mode"], "ocr")

    def test_document_overview_counts_per_page(self):
        with self.client(self.doc_answers()) as c:
            body = c.get("/api/documents/rid-12345678", headers=KEY).json()
        self.assertEqual(body["pages"], [
            {"page": 1, "source": "chandra", "error": None, "correction_count": 1, "unknown_count": 0},
            {"page": 2, "source": "chandra", "error": None, "correction_count": 0, "unknown_count": 1},
        ])
        self.assertEqual(body["unknown_words"][0]["word"], "qwxzv")
        self.assertNotIn("changes", body)

    def test_stored_document_exports_to_docx(self):
        import io

        from docx import Document

        pages = [{**p, "html": f"<p><b>Trang {p['page']}</b></p>"} for p in self.PAGES]
        answers = [("SELECT request_id", [self.SUMMARY + (pages, self.CHANGES)]), ("SELECT word", self.UNKNOWN)]
        with self.client(answers) as c:
            r = c.get("/api/documents/rid-12345678/export?format=docx", headers=KEY)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("attachment;", r.headers["content-disposition"])
        texts = [p.text for p in Document(io.BytesIO(r.content)).paragraphs if p.text]
        self.assertEqual(texts, ["Trang 1", "Trang 2"])

    def test_stored_document_without_html_cannot_be_exported(self):
        with self.client(self.doc_answers()) as c:
            self.assertEqual(c.get("/api/documents/rid-12345678/export?format=pdf", headers=KEY).status_code, 409)

    def test_page_lists_every_line_and_marks_the_changed_ones(self):
        with self.client(self.doc_answers()) as c:
            body = c.get("/api/documents/rid-12345678/pages/1", headers=KEY).json()
        first, second = body["lines"]
        self.assertEqual(first, {"line": 1, "text": "Dòng đầu", "changed": False})
        self.assertTrue(second["changed"])
        self.assertEqual((second["text"], second["before"]), ("Người được giao đất", "Người đưoc giao đất"))
        self.assertEqual(second["segments"][1], {"type": "fix", "before": "đưoc", "after": "được", "by": "protonx"})
        self.assertEqual(body["unknown_words"], [])

    def test_changes_can_be_filtered(self):
        with self.client(self.doc_answers()) as c:
            all_ = c.get("/api/documents/rid-12345678/changes", headers=KEY).json()
            page2 = c.get("/api/documents/rid-12345678/changes?page=2", headers=KEY).json()
            by_dict = c.get("/api/documents/rid-12345678/changes?by=dict", headers=KEY).json()
            bad = c.get("/api/documents/rid-12345678/changes?by=magic", headers=KEY)
        self.assertEqual(all_["total"], 1)
        self.assertEqual((page2["total"], by_dict["total"]), (0, 0))
        self.assertEqual(bad.status_code, 422)

    def test_missing_document_or_page_is_404(self):
        with self.client([]) as c:
            self.assertEqual(c.get("/api/documents/nope-1234", headers=KEY).status_code, 404)
        with self.client(self.doc_answers()) as c:
            self.assertEqual(c.get("/api/documents/rid-12345678/pages/9", headers=KEY).status_code, 404)

    def test_needs_a_database_and_an_api_key(self):
        with make_client(Mocks()) as c:
            self.assertEqual(c.get("/api/documents", headers=KEY).status_code, 503)
            self.assertEqual(c.get("/api/documents").status_code, 401)
            self.assertEqual(c.get("/api/documents/rid-12345678/pages/1").status_code, 401)


def make_wav(*parts, rate=16000, channels=1):
    """parts: (giây, có tiếng?) nối tiếp nhau."""
    import io
    import wave

    import numpy as np

    chunks = []
    for seconds, loud in parts:
        t = np.arange(int(seconds * rate)) / rate
        chunks.append((np.sin(2 * np.pi * 220 * t) * (8000 if loud else 0)).astype(np.int16))
    audio = np.concatenate(chunks)
    if channels > 1:
        audio = np.repeat(audio[:, None], channels, axis=1).reshape(-1)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(audio.tobytes())
    return buf.getvalue()


NO_FFMPEG = mock.patch("shutil.which", return_value=None)


class AudioDecode(unittest.TestCase):
    """Giải mã và cắt đoạn âm thanh."""

    def setUp(self):
        NO_FFMPEG.start()
        self.addCleanup(NO_FFMPEG.stop)
        self.dir = Path(os.environ["TEMP_DIR"]) / "audio-tests"
        self.dir.mkdir(exist_ok=True)

    def decode(self, data, name="a.wav"):
        from app.audio.decode import decode_to_pcm, load_pcm

        src = self.dir / name
        src.write_bytes(data)
        seconds = decode_to_pcm(src, self.dir / "a.pcm")
        return seconds, load_pcm(self.dir / "a.pcm")

    def test_wav_is_converted_to_16k_mono_without_ffmpeg(self):
        seconds, audio = self.decode(make_wav((3, True), rate=44100, channels=2))
        self.assertAlmostEqual(seconds, 3.0, places=2)
        self.assertEqual(len(audio), 48000)
        del audio

    def test_other_formats_need_ffmpeg(self):
        from app.audio.decode import UnsupportedAudio

        with self.assertRaises(UnsupportedAudio):
            self.decode(b"ID3 not really an mp3", "a.mp3")
        with self.assertRaises(UnsupportedAudio):
            self.decode(b"RIFF broken", "b.wav")

    def test_long_audio_is_cut_at_silence_and_silence_is_skipped(self):
        from app.audio.decode import SR, segments

        # 20s tiếng, 2s lặng, 20s tiếng, 30s lặng hẳn
        _, audio = self.decode(make_wav((20, True), (2, False), (20, True), (30, False)))
        parts = segments(audio, 15, 28, 300)
        del audio
        self.assertEqual(len(parts), 2)
        self.assertTrue(all((p["end"] - p["start"]) / SR <= 28 for p in parts))
        # Điểm cắt đầu tiên rơi vào khoảng lặng 20-22s
        self.assertTrue(20 <= parts[0]["end"] / SR <= 22)


class SpeechApi(unittest.TestCase):
    """Speech-to-text đồng bộ với PhoWhisper và ProtonX giả."""

    def setUp(self):
        NO_FFMPEG.start()
        self.addCleanup(NO_FFMPEG.stop)

    def post(self, mocks, data, name="ghi am.wav", **overrides):
        with make_client(mocks, **overrides) as client:
            return client.post("/api/speech-to-text/sync", headers=KEY, files={"file": (name, data, "audio/wav")},
                               data={"include_raw": "true"})

    def test_transcript_is_split_timed_and_spellchecked(self):
        mocks = Mocks(speech="Việc thưc hiện nghị quyết", spellfix=swap("thưc", "thực"))
        r = self.post(mocks, make_wav((20, True), (2, False), (20, True)))
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual((body["segment_count"], body["duration"]), (2, 42.0))
        self.assertEqual(body["text"], "Việc thực hiện nghị quyết\nViệc thực hiện nghị quyết")
        first = body["segments"][0]
        self.assertEqual((first["index"], first["file"], first["start"]), (1, "ghi am.wav", 0.0))
        self.assertEqual(first["raw_text"], "Việc thưc hiện nghị quyết")
        self.assertEqual(first["corrections"], [{"wrong": "thưc", "right": "thực", "by": "protonx"}])
        change = body["changes"][1]
        self.assertEqual((change["segment"], change["start"]), (2, body["segments"][1]["start"]))
        self.assertEqual(change["marked"], "Việc [thưc→thực] hiện nghị quyết")
        self.assertEqual(body["correction_total"], 2)
        self.assertEqual(body["input"], {"mode": "speech", "files": [{"file": "ghi am.wav", "duration": 42.0}]})
        self.assertEqual(len(mocks.speech_requests), 2)
        # Mặc định ProtonX xem mọi đoạn, kể cả đoạn không có từ lạ
        self.assertEqual(len(mocks.spellfix_requests[0]["texts"]), 2)

    def test_whisper_line_breaks_are_kept_on_one_line(self):
        r = self.post(Mocks(speech="Xin chào\nmọi người"), make_wav((5, True)), spell_mode="off")
        self.assertEqual(r.json()["text"], "Xin chào mọi người")

    def test_silent_audio_returns_an_empty_transcript(self):
        mocks = Mocks()
        r = self.post(mocks, make_wav((10, False)))
        self.assertEqual((r.status_code, r.json()["text"], r.json()["segment_count"]), (200, "", 0))
        self.assertIn("tiếng nói", r.json()["notice"])
        self.assertEqual(mocks.speech_requests, [])

    def test_backend_down_is_502_and_bad_file_is_415(self):
        self.assertEqual(self.post(Mocks(speech_status=503), make_wav((5, True)), spell_mode="off").status_code, 502)
        self.assertEqual(self.post(Mocks(), b"not audio", name="a.mp3").status_code, 415)

    def test_client_errors_are_not_retried(self):
        mocks = Mocks(speech_status=400)
        self.assertEqual(self.post(mocks, make_wav((5, True)), spell_mode="off").status_code, 502)
        self.assertEqual(len(mocks.speech_requests), 1)

    def test_too_long_audio_is_413(self):
        self.assertEqual(self.post(Mocks(), make_wav((70, True)), speech_max_minutes=1).status_code, 413)

    def test_async_submit_and_auth(self):
        body = {"s3_path": "s3://bucket/ghi-am/", "access_key": "AK", "secret_key": "SK", "endpoint": "http://minio:9000"}
        with make_client(Mocks()) as client, mock.patch("app.api.main.submit_speech") as submit:
            ok = client.post("/api/speech-to-text", headers=KEY, json=body)
            self.assertEqual(client.post("/api/speech-to-text", json=body).status_code, 401)
            self.assertEqual(client.post("/api/speech-to-text/sync", files={"file": ("a.wav", b"x")}).status_code, 401)
        self.assertEqual(ok.status_code, 202, ok.text)
        self.assertEqual(submit.call_args.args[0], ok.json()["request_id"])
        self.assertEqual(submit.call_args.args[1], "s3://bucket/ghi-am/")


class SpeechPipeline(unittest.TestCase):
    """Chạy 4 task Celery speech liên tiếp với S3 và mạng giả."""

    def setUp(self):
        celery_app.conf.task_always_eager = True
        celery_app.conf.task_eager_propagates = True
        self.hooks = []
        for patch in (NO_FFMPEG, mock.patch.dict(os.environ, Pipeline.ENV)):
            patch.start()
            self.addCleanup(patch.stop)

    def run_chain(self, mocks, files, s3_path, request_id):
        from app.worker import speech_tasks

        class Storage(FakeStorage):
            def __init__(self, *a, **k):
                super().__init__(files)

        with mock.patch.object(tasks, "S3Storage", Storage), \
             mock.patch.object(flow, "make_http_client", mocks.client), \
             mock.patch.object(tasks, "send_webhook", lambda url, payload, *a: self.hooks.append((url, payload)) or True):
            return speech_tasks.submit_speech(request_id, s3_path, "AK", "SK", "http://minio:9000", "http://hooks.example/cb", 9, True)

    def test_folder_of_audio_files_in_name_order(self):
        files = {"phan10.wav": make_wav((3, True)), "phan2.wav": make_wav((4, True)), "ghi-chu.txt": b"x"}
        result = self.run_chain(Mocks(speech="Xin chào"), files, "s3://bucket/hop/", "req-speech-0001").get()
        self.assertEqual([f["file"] for f in result["input"]["files"]], ["phan2.wav", "phan10.wav"])
        self.assertEqual([(s["index"], s["file"]) for s in result["segments"]], [(1, "phan2.wav"), (2, "phan10.wav")])
        self.assertEqual(result["duration"], 7.0)
        self.assertNotIn("raw_text", result["segments"][0])
        (url, payload), = self.hooks
        self.assertEqual((payload["meta"]["error_code"], payload["data"]["creator"]), (200, "speech-to-text"))
        self.assertFalse((Path(os.environ["TEMP_DIR"]) / "req-speech-0001").exists())

    def test_no_audio_file_sends_a_415_webhook(self):
        with self.assertRaises(Exception):
            self.run_chain(Mocks(), {"a.pdf": b"%PDF-"}, "s3://bucket/hop/", "req-speech-0002")
        self.assertEqual(self.hooks[0][1]["meta"]["error_code"], 415)

    def test_backend_down_sends_a_502_webhook(self):
        with self.assertRaises(Exception):
            self.run_chain(Mocks(speech_status=503), {"a.wav": make_wav((3, True))}, "s3://bucket/a.wav", "req-speech-0003")
        self.assertEqual(self.hooks[0][1]["meta"]["error_message"], "Speech-to-text backend unavailable")


class SpeechStorage(unittest.TestCase):
    """Lưu và đọc bản chép lời (DB giả)."""

    CREATED = __import__("datetime").datetime(2026, 11, 10, 8, 0)
    SEGMENTS = [
        {"index": 1, "file": "a.wav", "start": 0.0, "end": 21.0, "text": "Việc thực hiện", "error": None, "corrections": []},
        {"index": 2, "file": "a.wav", "start": 21.0, "end": 42.0, "text": "Xin chào", "error": None, "corrections": []},
    ]
    CHANGES = [{"segment": 1, "file": "a.wav", "start": 0.0, "end": 21.0, "before": "Việc thưc hiện", "after": "Việc thực hiện",
                "marked": "Việc [thưc→thực] hiện", "segments": [], "fixes": [{"wrong": "thưc", "right": "thực", "by": "dict"}]}]
    SUMMARY = ("rid-speech-01", "9", "s3://b/a.wav", 42.0, 2, 1, 0, None, {"mode": "speech"}, CREATED)

    def client(self, answers):
        conn = ScriptedConn(answers)
        patch = mock.patch("psycopg2.connect", return_value=conn)
        patch.start()
        self.addCleanup(patch.stop)
        return make_client(Mocks(), pg_host="h", pg_db="d", pg_user="u")

    def answers(self):
        return [("SELECT request_id", [self.SUMMARY + (self.SEGMENTS, self.CHANGES, [])])]

    def test_save_stores_each_fix_with_its_time(self):
        conn = FakeConn()
        result = {"duration": 42.0, "segment_count": 2, "text": "x", "segments": self.SEGMENTS, "correction_total": 1,
                  "changes": self.CHANGES, "unknown_total": 0, "unknown_words": [], "notice": None, "input": {}}
        with mock.patch("psycopg2.connect", return_value=conn):
            self.assertTrue(db.save_transcript(Settings(pg_host="h", pg_db="d", pg_user="u"), "rid", "s3://b/a.wav", 9, result))
        fixes = [params for sql, params in conn.log if sql.startswith("INSERT INTO speech_corrections")]
        self.assertEqual(fixes, [("rid", 1, 0.0, 21.0, "thưc", "thực", "dict", "Việc thưc hiện", "Việc thực hiện")])
        self.assertFalse(db.save_transcript(Settings(), "rid", "s3://b/a.wav", 9, result))

    def test_web_api(self):
        with self.client([("SELECT count(*)", [(1,)])] + self.answers()) as c:
            listing = c.get("/api/transcripts", headers=KEY).json()
            overview = c.get("/api/transcripts/rid-speech-01", headers=KEY).json()
            changed = c.get("/api/transcripts/rid-speech-01/segments?changed=true", headers=KEY).json()
            every = c.get("/api/transcripts/rid-speech-01/segments", headers=KEY).json()
            by_ai = c.get("/api/transcripts/rid-speech-01/changes?by=protonx", headers=KEY).json()
        self.assertEqual((listing["total"], listing["items"][0]["duration"]), (1, 42.0))
        self.assertNotIn("segments", overview)
        self.assertEqual(overview["segment_count"], 2)
        self.assertEqual(changed["total"], 1)
        self.assertEqual(changed["items"][0]["before"], "Việc thưc hiện")
        self.assertEqual([s["changed"] for s in every["items"]], [True, False])
        self.assertEqual(by_ai["total"], 0)

    def test_missing_transcript_and_no_database(self):
        with self.client([]) as c:
            self.assertEqual(c.get("/api/transcripts/nope-1234", headers=KEY).status_code, 404)
        with make_client(Mocks()) as c:
            self.assertEqual(c.get("/api/transcripts", headers=KEY).status_code, 503)


class Uploads(unittest.TestCase):
    """Tải file lên từ máy: nhiều file, cả thư mục, chạy hết chuỗi Celery."""

    def setUp(self):
        celery_app.conf.task_always_eager = True
        celery_app.conf.task_eager_propagates = True
        for patch in (NO_FFMPEG, mock.patch.dict(os.environ, Pipeline.ENV)):
            patch.start()
            self.addCleanup(patch.stop)
        self.upload_dir = Path(os.environ["TEMP_DIR"]) / "uploads"

    def post(self, path, files, mocks=None, **form):
        """Gửi upload; trả (response, kết quả của từng việc)."""
        from app.worker import speech_tasks

        results = []

        def run(submit):
            def wrapper(*args):
                r = submit(*args)
                results.append(r.get())
                return r
            return wrapper

        mocks = mocks or Mocks()
        with mock.patch("app.api.uploads.submit_fullpage", run(tasks.submit_fullpage)), \
             mock.patch("app.api.uploads.submit_speech", run(speech_tasks.submit_speech)), \
             mock.patch.object(flow, "make_http_client", mocks.client), \
             make_client(mocks, spell_mode="off", temp_dir=os.environ["TEMP_DIR"]) as client:
            data = {k: str(v).lower() for k, v in form.items()}
            r = client.post(path, headers=KEY, files=[("files", f) for f in files], data=data)
        return r, results

    def test_each_file_is_its_own_job_and_unsupported_files_are_skipped(self):
        files = [("a.pdf", make_pdf_pages(GOOD)), ("ghi-chu.txt", "Việc thực hiện nghị quyết".encode()), ("x.zip", b"PK")]
        r, results = self.post("/api/uploads/ocr", files)
        self.assertEqual(r.status_code, 202, r.text)
        body = r.json()
        self.assertEqual([j["files"] for j in body["jobs"]], [["a.pdf"], ["ghi-chu.txt"]])
        self.assertEqual(body["skipped"], [{"file": "x.zip", "reason": "unsupported file type"}])
        self.assertEqual([res["input"]["mode"] for res in results], ["ocr", "text"])
        self.assertEqual(results[1]["text"], "Việc thực hiện nghị quyết")
        self.assertEqual(results[0]["pages"][0]["source"], "pdf_text")
        # File tải lên được dọn sau khi xử lý
        self.assertEqual(list(self.upload_dir.iterdir()), [])

    def test_a_folder_is_one_document_when_grouped(self):
        # Thư mục ảnh scan, tên kèm đường dẫn như trình duyệt gửi
        files = [(f"so-1/trang{n}.png", make_png()) for n in (10, 2, 1)]
        r, results = self.post("/api/uploads/ocr", files, Mocks("<p>Trang</p>"), group=True)
        self.assertEqual(len(r.json()["jobs"]), 1)
        self.assertEqual(results[0]["input"]["files"], ["trang1.png", "trang2.png", "trang10.png"])
        self.assertEqual(results[0]["page_count"], 3)

    def test_word_file_beside_the_pdf_wins_when_grouped(self):
        files = [("a.pdf", make_pdf()), ("a.docx", make_docx(["Trang một"]))]
        _, results = self.post("/api/uploads/ocr", files, group=True)
        self.assertEqual(results[0]["input"]["mode"], "text")

    def test_speech_upload(self):
        files = [("phan2.wav", make_wav((3, True))), ("phan1.wav", make_wav((4, True))), ("anh.png", make_png())]
        r, results = self.post("/api/uploads/speech", files, Mocks(speech="Xin chào"), group=True)
        self.assertEqual(r.json()["skipped"][0]["file"], "anh.png")
        self.assertEqual([f["file"] for f in results[0]["input"]["files"]], ["phan1.wav", "phan2.wav"])
        self.assertEqual(results[0]["text"], "Xin chào\nXin chào")

    def test_bad_uploads(self):
        r, _ = self.post("/api/uploads/ocr", [("x.zip", b"PK")])
        self.assertEqual(r.status_code, 415)
        r, _ = self.post("/api/uploads/ocr", [("a.txt", b"")])
        self.assertEqual(r.status_code, 422)
        with make_client(Mocks(), max_upload_files=1, temp_dir=os.environ["TEMP_DIR"]) as client:
            many = client.post("/api/uploads/ocr", headers=KEY, files=[("files", ("a.txt", b"x")), ("files", ("b.txt", b"y"))])
            self.assertEqual(many.status_code, 413)
            self.assertEqual(client.post("/api/uploads/ocr", files=[("files", ("a.txt", b"x"))]).status_code, 401)

    def test_names_are_made_safe(self):
        from app.storage.local import LocalStorage, safe_name

        self.assertEqual(safe_name("../../etc/passwd"), "passwd")
        self.assertEqual(safe_name("C:\\\\tai lieu\\\\.hidden.pdf"), "hidden.pdf")
        storage = LocalStorage(self.upload_dir)
        for bad in ("upload://../x/", "upload://abc/", "upload://" + "a" * 32 + "/../x"):
            with self.assertRaises(ValueError):
                storage.candidates(bad)


INFO_PAGE = """NHÀ XUẤT BẢN CHÍNH TRỊ QUỐC GIA SỰ THẬT
Địa chỉ: Số 6/86 Duy Tân, Cầu Giấy, Hà Nội
ĐT: 080.49221, Fax: 080.49222
Chịu trách nhiệm xuất bản
GIÁM ĐỐC - TỔNG BIÊN TẬP
PGS.TS. PHẠM MINH TUẤN
Biên tập nội dung: ThS. CÙ THỊ THÚY LAN
NGUYỄN THU HƯỜNG
Trình bày bìa: VƯƠNG THANH TÚ
Chế bản vi tính: VƯƠNG THANH TÚ
Số đăng ký kế hoạch xuất bản: 470-2021/CXBIPH/2-14/CTQG.
Số xác nhận đăng ký xuất bản ngày 05/3/2021.
Mã số ISBN: 978-604-57-6547-0
In 1.000 cuốn, khổ 14,5x20,5 cm, tại Công ty TNHH In Thanh Bình
Địa chỉ: Số 18 ngách 68/53/9 - P. Quan Hoa - Q. Cầu Giấy - Hà Nội
In xong và nộp lưu chiểu tháng 3 năm 2021."""
# Font PDF chuẩn không có chữ Việt: bản không dấu
INFO_ASCII = "\n".join(__import__("app.book.fields", fromlist=["plain"]).plain(line) for line in INFO_PAGE.split("\n"))


class BookFields(unittest.TestCase):
    """Tách thông tin xuất bản ở trang cuối."""

    def test_publication_block(self):
        from app.book.fields import count, extract

        f = extract(INFO_PAGE)
        self.assertEqual(f["nha_xuat_ban"], "NHÀ XUẤT BẢN CHÍNH TRỊ QUỐC GIA SỰ THẬT")
        self.assertEqual(f["isbn"], "978-604-57-6547-0")
        self.assertEqual((f["luu_chieu_thang"], f["luu_chieu_nam"]), (3, 2021))
        self.assertEqual(f["giam_doc_tong_bien_tap"], "PGS.TS. PHẠM MINH TUẤN")
        self.assertEqual(f["ban_bien_tap"], "ThS. CÙ THỊ THÚY LAN; NGUYỄN THU HƯỜNG")
        self.assertEqual((f["trinh_bay_bia"], f["che_ban"]), ("VƯƠNG THANH TÚ", "VƯƠNG THANH TÚ"))
        self.assertEqual(f["dia_chi_nxb"], "Số 6/86 Duy Tân, Cầu Giấy, Hà Nội")
        self.assertEqual(f["dia_chi_co_so_in"], "Số 18 ngách 68/53/9 - P. Quan Hoa - Q. Cầu Giấy - Hà Nội")
        self.assertEqual((f["dien_thoai_nxb"], f["fax_nxb"]), ("080.49221", "080.49222"))
        self.assertEqual(f["ngay_xac_nhan_dkxb"], "05/03/2021")
        self.assertGreaterEqual(count(f), 10)

    def test_deposit_date_variants(self):
        from app.book.fields import extract

        cases = {
            "In xong và nộp lưu chiểu tháng 03/2021.": (3, 2021),
            "In xong và nộp lưu chiểu T3/2021": (3, 2021),
            "In xong và nộp lưu chiểu tháng ba năm 2021": (3, 2021),
            "In xong và nộp lưu chiểu tháng 12-20": (12, 2020),
            "In xong nop lou chieau thang 5 nam 2019": (5, 2019),
            "In xong và nộp lưu chiểu\ntháng 7 năm 2022": (7, 2022),
            "In xong và nộp lưu chiểu quý I năm 2021": (None, 2021),
            "Quyết định xuất bản số 79-QĐ/NXBCTQG ngày 05/3/2021\nIn xong và nộp lưu chiểu năm 2021": (3, 2021),
            "Giấy phép xuất bản ngày 05/3/2021": (None, None),
        }
        for text, expected in cases.items():
            f = extract(text)
            self.assertEqual((f["luu_chieu_thang"], f["luu_chieu_nam"]), expected, text)


class BookMetadata(unittest.TestCase):
    """Metadata sách từ file tải lên / S3 (giả)."""

    def setUp(self):
        celery_app.conf.task_always_eager = True
        celery_app.conf.task_eager_propagates = True
        for patch in (NO_FFMPEG, mock.patch.dict(os.environ, Pipeline.ENV)):
            patch.start()
            self.addCleanup(patch.stop)

    def sync(self, files, mocks=None, **overrides):
        mocks = mocks or Mocks()
        with make_client(mocks, temp_dir=os.environ["TEMP_DIR"], **overrides) as client:
            r = client.post("/api/book-metadata/sync", headers=KEY, files=[("files", f) for f in files])
        return r, mocks

    def test_word_last_page_is_used_without_ocr(self):
        body_pages = [["Chương 1", "Nội dung sách"], ["Tài liệu tham khảo", "Nxb. Văn hóa, Hà Nội, 2002, tr.183"],
                      INFO_PAGE.split("\n")]
        r, mocks = self.sync([("sach.pdf", make_pdf()), ("sach.docx", make_docx(*body_pages))])
        body = r.json()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((body["source"], body["file"], body["page"]), ("text_file", "sach.docx", 3))
        self.assertEqual(body["thong_tin"]["isbn"], "978-604-57-6547-0")
        # Dòng tài liệu tham khảo ở trang khác không bị lấy làm nhà xuất bản
        self.assertEqual(body["thong_tin"]["nha_xuat_ban"], "NHÀ XUẤT BẢN CHÍNH TRỊ QUỐC GIA SỰ THẬT")
        self.assertEqual(mocks.chandra_requests, [])

    def test_pdf_text_layer_is_used_when_clean(self):
        # Bản không dấu không có trong từ điển: bỏ ngưỡng từ điển
        r, mocks = self.sync([("sach.pdf", make_pdf_pages(GOOD, GOOD, INFO_ASCII))], pdf_text_min_known=0.0)
        body = r.json()
        self.assertEqual((body["source"], body["page"], body["page_count"]), ("pdf_text", 3, 3))
        self.assertEqual((body["thong_tin"]["luu_chieu_thang"], body["thong_tin"]["luu_chieu_nam"]), (3, 2021))
        self.assertEqual(mocks.chandra_requests, [])

    def test_bad_text_layer_goes_to_ocr_from_the_last_page(self):
        html = "".join(f"<p>{line}</p>" for line in INFO_PAGE.split("\n"))
        r, mocks = self.sync([("sach.pdf", make_pdf_pages(TCVN3_LINE, GOOD, GOOD))], Mocks(html))
        body = r.json()
        self.assertEqual((body["source"], body["page"]), ("ocr", 3))
        self.assertIn("TCVN3", body["input"]["pdf_text"])
        self.assertEqual(body["thong_tin"]["isbn"], "978-604-57-6547-0")
        # Trang cuối đã là trang thông tin: dừng, không OCR thêm
        self.assertEqual(len(mocks.chandra_requests), 1)

    def test_folder_of_images_reads_the_last_images(self):
        html = "".join(f"<p>{line}</p>" for line in INFO_PAGE.split("\n"))
        files = [(f"trang{n}.png", make_png()) for n in (1, 2, 10)]
        r, mocks = self.sync(files, Mocks(html), book_last_pages=2)
        body = r.json()
        self.assertEqual((body["source"], body["file"], body["page"]), ("ocr", "trang10.png", 3))
        self.assertEqual(len(mocks.chandra_requests), 1)

    def test_nothing_found_is_not_an_error(self):
        r, _ = self.sync([("ghi-chu.txt", "Việc thực hiện nghị quyết".encode())])
        body = r.json()
        self.assertEqual((r.status_code, body["field_count"], body["error"]), (200, 0, "không tìm thấy thông tin xuất bản"))
        self.assertEqual(self.sync([("x.zip", b"PK")])[0].status_code, 415)

    def test_upload_queue_and_s3_submit(self):
        from app.worker import book_tasks

        results = []

        def run(*args):
            r = book_tasks.submit_book(*args)
            results.append(r.get())
            return r

        with mock.patch("app.api.uploads.submit_book", run), mock.patch.dict(os.environ, {"PDF_TEXT_MIN_KNOWN": "0"}), \
             make_client(Mocks(), temp_dir=os.environ["TEMP_DIR"]) as client:
            r = client.post("/api/uploads/metadata", headers=KEY,
                            files=[("files", ("phan2.pdf", make_pdf_pages(GOOD, INFO_ASCII))), ("files", ("phan1.pdf", make_pdf()))])
        self.assertEqual(len(r.json()["jobs"]), 1)
        # PDF cuối theo tên chứa trang cuối sách
        self.assertEqual((results[0]["source"], results[0]["file"]), ("pdf_text", "phan2.pdf"))
        with make_client(Mocks()) as client, mock.patch("app.api.main.submit_book") as submit:
            ok = client.post("/api/book-metadata", headers=KEY, json={"s3_path": "s3://b/sach/", "access_key": "A",
                                                                   "secret_key": "S", "endpoint": "http://minio:9000"})
        self.assertEqual(ok.status_code, 202)
        self.assertEqual(submit.call_args.args[1], "s3://b/sach/")

    def test_saved_and_listed(self):
        conn = FakeConn()
        result = {"thong_tin": {"isbn": "978"}, "field_count": 1, "source": "pdf_text", "file": "a.pdf", "page": 3,
                  "text": "x", "error": "", "input": {}}
        with mock.patch("psycopg2.connect", return_value=conn):
            self.assertTrue(db.save_book(Settings(pg_host="h", pg_db="d", pg_user="u"), "rid", "s3://b/a.pdf", 1, result))
        self.assertTrue(any(sql.startswith("INSERT INTO book_metadata") for sql, _ in conn.log))
        row = ("rid-book-0001", "1", "s3://b/a.pdf", {"isbn": "978"}, 1, "pdf_text", "a.pdf", 3, "x", "", {},
               DocumentsApi.CREATED)
        answers = [("SELECT count(*)", [(1,)]), ("SELECT request_id", [row])]
        with mock.patch("psycopg2.connect", return_value=ScriptedConn(answers)), \
             make_client(Mocks(), pg_host="h", pg_db="d", pg_user="u") as c:
            listing = c.get("/api/book-metadata", headers=KEY).json()
            one = c.get("/api/book-metadata/rid-book-0001", headers=KEY).json()
        self.assertEqual(listing["items"][0]["fields"], {"isbn": "978"})
        self.assertEqual(one["page"], 3)


class Config(unittest.TestCase):
    def test_max_pages_zero_means_no_limit(self):
        from app.pipeline.pages import PageSource, TooManyPages

        doc = fitz.open()
        for _ in range(150):
            doc.new_page()
        data = doc.tobytes()
        doc.close()
        base = {"CELERY_BROKER_URL": "memory://", "CELERY_RESULT_BACKEND": "cache+memory://"}
        with mock.patch.dict(os.environ, {**base, "MAX_PAGES": "0"}):
            unlimited = load_settings().max_pages
        self.assertEqual(PageSource(data, 72, unlimited).count, 150)
        with mock.patch.dict(os.environ, {**base, "MAX_PAGES": "100"}):
            limited = load_settings().max_pages
        with self.assertRaises(TooManyPages):
            PageSource(data, 72, limited)

    def test_refuses_to_start_without_login(self):
        with self.assertRaises(RuntimeError):
            create_app(Settings())
        with self.assertRaises(RuntimeError):
            create_app(Settings(api_username="admin"))

    def test_secrets_are_not_in_repr(self):
        s = Settings(
            api_username="admin", api_password="k1secret", chandra_api_key="t1secret",
            broker_url="amqp://u:p3secret@h//", result_backend="db+postgresql://u:p4secret@h/db",
            pg_password="p5secret", flower_auth="u:p6secret", s3_access_key="p7secret", s3_secret_key="p8secret",
        )
        text = repr(s)
        for secret in ("k1secret", "t1secret", "p3secret", "p4secret", "p5secret", "p6secret", "p7secret", "p8secret"):
            self.assertNotIn(secret, text)

    def test_s3_endpoint_gets_a_scheme_from_s3_ssl(self):
        from app.storage.s3 import endpoint_url

        self.assertEqual(endpoint_url("minio:9000", False), "http://minio:9000")
        self.assertEqual(endpoint_url("minio:9000", True), "https://minio:9000")
        self.assertEqual(endpoint_url("http://minio:9000", True), "http://minio:9000")

    def test_invalid_settings_are_rejected(self):
        for name, value in (("SPELL_MODE", "maybe"), ("SPELL_REVIEW", "x"), ("UNKNOWN_INCLUDE", "vietnamese,bogus")):
            with mock.patch.dict(os.environ, {name: value}), self.assertRaises(ValueError):
                load_settings()

    def test_dictionary_selection_comes_from_the_environment(self):
        with mock.patch.dict(os.environ, {"DICTIONARIES": "hongocduc, tudientv"}):
            self.assertEqual(load_settings().dictionaries, ("hongocduc", "tudientv"))

    def test_no_secret_defaults_in_source(self):
        src = "\n".join(p.read_text(encoding="utf-8") for p in Path(ROOT / "app").rglob("*.py")).lower()
        for needle in ("password=\"", "cntt@", "ocop@123", "trytouch", "guest:guest"):
            self.assertNotIn(needle, src)

    def test_vintern_is_gone(self):
        for path in list(Path(ROOT / "app").rglob("*.py")) + [ROOT / ".env", ROOT / "requirements.txt"]:
            self.assertNotIn("vintern", path.read_text(encoding="utf-8").lower(), path.name)
        self.assertFalse((ROOT / "service" / "vllm-vintern.service").exists())

    def test_qwen_and_the_qldd_flows_are_gone(self):
        files = list(Path(ROOT / "app").rglob("*.py")) + [ROOT / n for n in (".env", "requirements.txt", "start-service.sh")]
        for path in files:
            text = path.read_text(encoding="utf-8").lower()
            for stale in ("qwen", "qldd", "detectron", "phieutin", "tasks_new"):
                self.assertNotIn(stale, text, f"{path.name}: {stale}")
        for name in ("flows_new.py", "flow_phieutin.py", "tasks_new.py", "vision_service.py", "detectron2_api.py"):
            self.assertFalse((ROOT / "app" / name).exists(), name)

    def test_queue_names_are_consistent(self):
        from app.worker.manager import QUEUE_LOG_SUBDIR, WORKER_PRESETS

        self.assertEqual(set(QUEUE_LOG_SUBDIR), set(QUEUES))
        self.assertEqual(set(WORKER_PRESETS), set(QUEUES))
        self.assertEqual({q.name for q in celery_app.conf.task_queues}, set(QUEUES))


if __name__ == "__main__":
    unittest.main()
