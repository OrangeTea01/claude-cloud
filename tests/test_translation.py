import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from translation.app import create_app
from translation.download_models import FILES, install_archive
from translation.engine import MAX_PROSE_CHUNK, MAX_TEXT, prose_chunks, segments, translate_preserving


class PreservationTests(unittest.TestCase):
    def run_translation(self, text, source="en", target="zh"):
        calls = []

        def translate(chunk):
            calls.append(chunk)
            return "译文"

        return translate_preserving(text, source, target, translate), calls

    def test_code_fences_copied_exactly_and_never_enter_model(self):
        code = '```python\r\nmessage = "Hello 世界"\r\nprint(message)\r\n```\r\n'
        output, calls = self.run_translation("Read:\r\n" + code + "Finished.")
        self.assertIn(code, output)
        self.assertEqual(calls, ["Read:", "Finished."])

    def test_unclosed_fence_stays_literal(self):
        code = "```sh\nrm -rf /workspace/test\n"
        output, calls = self.run_translation("Code:\n" + code)
        self.assertTrue(output.endswith(code))
        self.assertEqual(calls, ["Code:"])

    def test_tilde_fence_and_long_inline_delimiter(self):
        text = "Keep ``a ` b`` and this:\n~~~js\nconst a = `x`;\n~~~\n"
        output, calls = self.run_translation(text)
        self.assertIn("``a ` b``", output)
        self.assertIn("~~~js\nconst a = `x`;\n~~~\n", output)
        self.assertFalse(any("const" in item or "a ` b" in item for item in calls))

    def test_paths_urls_files_flags_and_identifiers_survive(self):
        literals = ["/workspace/src/app.py", "C:\\Users\\example\\data.txt", "src/main.ts", "https://example.com/api?q=one", "package.json", "--dry-run", "MY_SETTING"]
        text = "Use " + " and ".join(literals) + "."
        output, calls = self.run_translation(text)
        for literal in literals:
            self.assertIn(literal, output)
            self.assertFalse(any(literal in item for item in calls))

    def test_urls_leave_following_chinese_punctuation_for_prose(self):
        chunks = list(segments("看 https://example.com/api。然后继续。"))
        self.assertIn(("https://example.com/api", True), chunks)
        self.assertEqual("".join(value for value, _ in chunks), "看 https://example.com/api。然后继续。")

    def test_filename_next_to_chinese_still_protected(self):
        output, calls = self.run_translation("请修改app.py文件和src/main.ts文件", "zh", "en")
        self.assertIn("app.py", output)
        self.assertIn("src/main.ts", output)
        self.assertFalse(any("app.py" in chunk or "src/main.ts" in chunk for chunk in calls))

    def test_unicode_paths_are_kept_out_of_the_model(self):
        literals = ["/workspace/中文目录/文件.txt", "src/中文目录/文件.py", "目录/文件.txt", "C:\\文件\\项目.py"]
        output, calls = self.run_translation("请修改 " + "，以及 ".join(literals) + "。", "zh", "en")
        for literal in literals:
            self.assertIn(literal, output)
            self.assertFalse(any(literal in item for item in calls))

    def test_english_words_do_not_join_protected_literals(self):
        output = translate_preserving("修改`src/app.py`文件", "zh", "en", lambda text: "Modify" if text == "修改" else "file")
        self.assertEqual(output, "Modify `src/app.py` file")

    def test_english_input_not_translated_in_zh_to_en_route(self):
        output, calls = self.run_translation("Keep this English text `exactly`.", "zh", "en")
        self.assertEqual(output, "Keep this English text `exactly`.")
        self.assertEqual(calls, [])

    def test_chinese_only_reply_not_translated_again(self):
        output, calls = self.run_translation("已经完成。\n这是中文。")
        self.assertEqual(output, "已经完成。\n这是中文。")
        self.assertEqual(calls, [])

    def test_markdown_bullets_newlines_and_indentation_preserved(self):
        output, calls = self.run_translation("# Title\n\n- First\n  2. Second\r\n> Quote\n")
        self.assertEqual(output, "# 译文\n\n- 译文\n  2. 译文\r\n> 译文\n")
        self.assertEqual(calls, ["Title", "First", "Second", "Quote"])

    def test_long_prose_is_bounded_without_dropping_characters(self):
        text = ("测试句子。" * 300) + "x" * 1000 + "\n  end  "
        chunks = list(prose_chunks(text))
        self.assertEqual("".join(chunk for chunk, _ in chunks), text)
        self.assertTrue(all(len(chunk) <= MAX_PROSE_CHUNK for chunk, active in chunks if active))
        self.assertEqual(translate_preserving(text, "zh", "en", lambda value: value), text)

    def test_invalid_direction_and_oversize_input_fail(self):
        for source, target in [("zh", "zh"), ("en", "fr")]:
            with self.assertRaises(ValueError):
                translate_preserving("Hello", source, target, str.upper)
        with self.assertRaises(ValueError):
            translate_preserving("a" * (MAX_TEXT + 1), "en", "zh", str.upper)

    def test_empty_and_oversize_model_outputs_fail(self):
        with self.assertRaises(RuntimeError):
            translate_preserving("Hello", "en", "zh", lambda value: "")
        with self.assertRaises(ValueError):
            translate_preserving("Hello", "en", "zh", lambda value: "a" * 256001)

    def test_code_only_message_needs_no_model_call(self):
        text = "```python\nprint('你好')\n```"
        output, calls = self.run_translation(text, "zh", "en")
        self.assertEqual(output, text)
        self.assertEqual(calls, [])


class FakeEngine:
    def __init__(self):
        self.ready = False
        self.fail = False
        self.calls = []

    def warm(self):
        self.ready = True

    def translate(self, text, source, target):
        self.calls.append((text, source, target))
        if self.fail:
            raise RuntimeError("Sensitive input must never be reflected: " + text)
        return "translated"


class TranslationApiTests(unittest.TestCase):
    def setUp(self):
        self.engine = FakeEngine()
        self.client = TestClient(create_app(self.engine))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def test_ready_and_translation_contract(self):
        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["languages"], ["zh", "en"])
        result = self.client.post("/translate", json={"text": "你好", "source": "zh", "target": "en"})
        self.assertEqual(result.json(), {"text": "translated"})
        self.assertEqual(self.engine.calls, [("你好", "zh", "en")])
        self.assertEqual(result.headers["cache-control"], "no-store")

    def test_unready_does_not_return_untranslated_success(self):
        self.engine.ready = False
        self.assertEqual(self.client.get("/health").status_code, 503)
        result = self.client.post("/translate", json={"text": "你好", "source": "zh", "target": "en"})
        self.assertEqual(result.status_code, 503)
        self.assertEqual(self.engine.calls, [])

    def test_model_error_is_redacted_and_not_success(self):
        self.engine.fail = True
        result = self.client.post("/translate", json={"text": "secret-message", "source": "en", "target": "zh"})
        self.assertEqual(result.status_code, 503)
        self.assertNotIn("secret-message", result.text)
        self.assertNotIn("text", result.json())

    def test_invalid_direction_and_large_body_rejected(self):
        result = self.client.post("/translate", json={"text": "hello", "source": "en", "target": "en"})
        self.assertEqual(result.status_code, 422)
        result = self.client.post("/translate", content=b"x" * 512001, headers={"Content-Type": "application/json"})
        self.assertEqual(result.status_code, 413)
        self.assertEqual(self.engine.calls, [])

    def test_validation_does_not_reflect_source_text(self):
        result = self.client.post("/translate", json={"text": "secret-message", "source": "bad", "target": "zh"})
        self.assertEqual(result.status_code, 422)
        self.assertNotIn("secret-message", result.text)


class ModelArchiveTests(unittest.TestCase):
    def test_checksum_failure_never_extracts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "bad.zip"
            archive.write_bytes(b"invalid")
            with self.assertRaisesRegex(ValueError, "checksum"):
                install_archive(archive, root / "output", {"sha256": "0" * 64})
            self.assertFalse((root / "output").exists())

    def test_only_explicit_model_members_are_copied(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "model.zip"
            with zipfile.ZipFile(archive, "w") as file:
                for member in FILES:
                    data = json.dumps({"from_code": "en", "to_code": "zh", "package_version": "1.9"}) if member == "metadata.json" else "model-data"
                    file.writestr("model-root/" + member, data)
                file.writestr("../../escaped.txt", "unsafe")
                file.writestr("model-root/unused.py", "unsafe")
            spec = {"sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "archive_root": "model-root", "source": "en", "target": "zh", "version": "1.9"}
            output = root / "output"
            install_archive(archive, output, spec)
            self.assertEqual({p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()}, set(FILES))
            self.assertFalse((root / "escaped.txt").exists())


if __name__ == "__main__":
    unittest.main()
