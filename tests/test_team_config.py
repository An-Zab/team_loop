# -*- coding: utf-8 -*-
"""Тесты для team_config.py (ШАГ 1)."""

import io
import math
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# Добавляем корень репозитория в sys.path
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import team_config as tc


class TestParseEnvText(unittest.TestCase):
    """Тестирование dotenv-совместимого парсера .env."""

    def test_comment_and_empty_value(self):
        text = "KEY= # comment\n"
        parsed = tc._parse_env_text(text)
        self.assertEqual(parsed.get("KEY"), "")

    def test_comment_with_tab(self):
        text = "KEY=8192\t# ok\n"
        parsed = tc._parse_env_text(text)
        self.assertEqual(parsed.get("KEY"), "8192")

    def test_export_with_tab_and_space(self):
        text = "export\tKEY1=val1\nexport   KEY2=val2\n"
        parsed = tc._parse_env_text(text)
        self.assertEqual(parsed.get("KEY1"), "val1")
        self.assertEqual(parsed.get("KEY2"), "val2")

    def test_single_quoted_values_and_escapes(self):
        text = "K1='hello world'\nK2='single\\'quote'\n"
        parsed = tc._parse_env_text(text)
        self.assertEqual(parsed.get("K1"), "hello world")
        self.assertEqual(parsed.get("K2"), "single'quote")

    def test_double_quoted_values_and_escapes(self):
        text = 'K1="hello\\nworld"\nK2="tab\\there"\nK3="quote\\"inside"\n'
        parsed = tc._parse_env_text(text)
        self.assertEqual(parsed.get("K1"), "hello\nworld")
        self.assertEqual(parsed.get("K2"), "tab\there")
        self.assertEqual(parsed.get("K3"), 'quote"inside')

    def test_unclosed_quotes_ignored_safely(self):
        text = 'K1="unclosed\nK2=valid\n'
        parsed = tc._parse_env_text(text)
        self.assertNotIn("K1", parsed)
        self.assertEqual(parsed.get("K2"), "valid")

    def test_multiline_quotes(self):
        text = 'K1="line1\nline2"\nK2=after\n'
        parsed = tc._parse_env_text(text)
        self.assertEqual(parsed.get("K1"), "line1\nline2")
        self.assertEqual(parsed.get("K2"), "after")

    def test_hash_without_whitespace_preserved(self):
        text = "URL=http://localhost:1234/v1#frag\nTOUCH=pass#word\n"
        parsed = tc._parse_env_text(text)
        self.assertEqual(parsed.get("URL"), "http://localhost:1234/v1#frag")
        self.assertEqual(parsed.get("TOUCH"), "pass#word")

    def test_clean_value_helper(self):
        self.assertEqual(tc._clean_value(" # comment"), "")
        self.assertEqual(tc._clean_value("8192\t# ok"), "8192")
        self.assertEqual(tc._clean_value('"quoted # val" # comment'), "quoted # val")
        self.assertEqual(tc._clean_value('"unclosed'), "")
        self.assertEqual(tc._clean_value("pass#word"), "pass#word")
        self.assertEqual(tc._clean_value("pass #word"), "pass")


class TestWarnRobustness(unittest.TestCase):
    """Тестирование надежности функции _warn()."""

    def setUp(self):
        self.orig_stderr = sys.stderr

    def tearDown(self):
        sys.stderr = self.orig_stderr

    def test_warn_with_none_stderr(self):
        sys.stderr = None
        # Не должно вызывать исключений
        tc._warn("Тестовое предупреждение при sys.stderr is None")

    def test_warn_with_broken_stderr_stream(self):
        class BrokenStream:
            def write(self, data):
                raise OSError("Broken pipe")

            def flush(self):
                raise OSError("Flush error")

        sys.stderr = BrokenStream()
        tc._warn("Тестовое предупреждение при сломанном потоке")

    def test_warn_with_unicode_encode_error_fallback(self):
        class EncodingFallbackStream:
            def __init__(self):
                self.calls = []

            def write(self, data):
                self.calls.append(data)
                if any(ord(c) > 127 for c in data):
                    raise UnicodeEncodeError("ascii", data, 0, 1, "test")

            def flush(self):
                pass

        stream = EncodingFallbackStream()
        sys.stderr = stream
        tc._warn("Предупреждение с кириллицей")
        # Первая попытка упала с UnicodeEncodeError, вторая прошла в ASCII
        self.assertGreaterEqual(len(stream.calls), 2)
        self.assertTrue(all(ord(c) < 128 for c in stream.calls[-1]))


class TestTokenCalculations(unittest.TestCase):
    """Тестирование функций extract_used_tokens и is_context_critical."""

    def test_extract_used_tokens_from_dict_usage(self):
        response = {"usage": {"total_tokens": 1234}}
        self.assertEqual(tc.extract_used_tokens(response, []), 1234)

    def test_extract_used_tokens_zero(self):
        response = {"usage": {"total_tokens": 0}}
        self.assertEqual(tc.extract_used_tokens(response, []), 0)

    def test_extract_used_tokens_from_object_usage(self):
        class FakeUsage:
            total_tokens = 5678

        class FakeResponse:
            usage = FakeUsage()

        self.assertEqual(tc.extract_used_tokens(FakeResponse(), []), 5678)

    def test_extract_used_tokens_fallback_messages(self):
        messages = [
            {"role": "user", "content": "x" * 300},
            ("assistant", "y" * 300),
        ]
        # 600 символов // TOKEN_ESTIMATE_DIVISOR (3) = 200
        got = tc.extract_used_tokens(None, messages)
        self.assertEqual(got, 600 // tc.TOKEN_ESTIMATE_DIVISOR)

    def test_is_context_critical_threshold(self):
        limit = tc.HANDOFF_TOKEN_LIMIT
        self.assertFalse(tc.is_context_critical(limit - 1))
        self.assertTrue(tc.is_context_critical(limit))
        self.assertTrue(tc.is_context_critical(limit + 1))

    def test_is_context_critical_runtime_clamp(self):
        saved = tc.HANDOFF_TOKEN_LIMIT
        try:
            # Занижение в 0 не должно приводить к срабатыванию при 0 токенов
            tc.HANDOFF_TOKEN_LIMIT = 0
            self.assertFalse(tc.is_context_critical(0))
            self.assertTrue(tc.is_context_critical(1))

            # Завышение за пределы окна зажимается в MAX_CONTEXT_TOKENS
            tc.HANDOFF_TOKEN_LIMIT = tc.MAX_CONTEXT_TOKENS + 100000
            self.assertFalse(tc.is_context_critical(tc.MAX_CONTEXT_TOKENS - 1))
            self.assertTrue(tc.is_context_critical(tc.MAX_CONTEXT_TOKENS))
        finally:
            tc.HANDOFF_TOKEN_LIMIT = saved


class TestConfigBounds(unittest.TestCase):
    """Тестирование диапазонов и дефолтов конфигурации."""

    def test_max_context_tokens_bounds(self):
        self.assertGreaterEqual(tc.MAX_CONTEXT_TOKENS, 1)
        self.assertLessEqual(tc.MAX_CONTEXT_TOKENS, 131072)

    def test_handoff_free_ratio_bounds(self):
        self.assertGreater(tc.HANDOFF_FREE_RATIO, 0.0)
        self.assertLess(tc.HANDOFF_FREE_RATIO, 1.0)
        self.assertFalse(math.isnan(tc.HANDOFF_FREE_RATIO))
        self.assertFalse(math.isinf(tc.HANDOFF_FREE_RATIO))

    def test_handoff_token_limit_bounds(self):
        self.assertGreaterEqual(tc.HANDOFF_TOKEN_LIMIT, 1)
        self.assertLessEqual(tc.HANDOFF_TOKEN_LIMIT, tc.MAX_CONTEXT_TOKENS)

    def test_divisor_safe_against_bool(self):
        saved = tc.TOKEN_ESTIMATE_DIVISOR
        try:
            tc.TOKEN_ESTIMATE_DIVISOR = True  # bool не должен считаться целым 1
            self.assertEqual(tc._estimate_divisor(), 3)
        finally:
            tc.TOKEN_ESTIMATE_DIVISOR = saved


class TestPathsAndEncoding(unittest.TestCase):
    """Тестирование путей и кодировок .env."""

    def test_handoff_files_absolute(self):
        self.assertTrue(tc.HANDOFF_FILE.is_absolute())
        self.assertTrue(tc.HANDOFF_BACKUP.is_absolute())

    def test_decode_env_text_utf8(self):
        data = "MODEL_NAME=utf8-model\n".encode("utf-8")
        text = tc._decode_env_text(data, Path(".env"))
        self.assertIsNotNone(text)
        self.assertIn("MODEL_NAME=utf8-model", text)

    def test_decode_env_text_utf16_bom(self):
        data = "\ufeffMODEL_NAME=utf16-model\n".encode("utf-16")
        text = tc._decode_env_text(data, Path(".env"))
        self.assertIsNotNone(text)
        self.assertIn("MODEL_NAME=utf16-model", text)

    def test_decode_env_text_cp1251_rejected_cleanly(self):
        data = "MODEL_NAME=модель\n".encode("cp1251")
        text = tc._decode_env_text(data, Path(".env"))
        self.assertIsNone(text)

    def test_decode_env_text_nul_bytes_rejected_cleanly(self):
        data = "MODEL_NAME=nul\x00model\n".encode("utf-8")
        text = tc._decode_env_text(data, Path(".env"))
        self.assertIsNone(text)


if __name__ == "__main__":
    unittest.main()
