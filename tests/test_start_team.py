# -*- coding: utf-8 -*-
"""Тесты интерактивного запуска start_team.py (шаги 2-4 без консоли и сети)."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import start_team as st


VALID_HANDOFF = (
    "# Исходная задача\nЗавершить проект\n\n"
    "# Статус плана\n- [ ] Проверить модуль\n\n"
    "# Артефакты и файлы на диске\nmodule.py\n\n"
    "# Текущий контекст и блокер\nПродолжить проверку.\n"
)


class TestModelsEndpoint(unittest.TestCase):
    def test_base_url_with_v1(self):
        self.assertEqual(
            st.models_endpoint("http://localhost:8080/v1"),
            "http://localhost:8080/v1/models",
        )

    def test_base_url_with_trailing_slash_and_without_v1(self):
        self.assertEqual(
            st.models_endpoint("http://localhost:11434/"),
            "http://localhost:11434/models",
        )

    def test_empty_base_url(self):
        self.assertEqual(st.models_endpoint(""), "")

    def test_unreachable_server_means_model_not_loaded(self):
        # Порт 1 заведомо никого не слушает: «не загружена» — это и есть ответ.
        self.assertIsNone(st.query_loaded_model("http://127.0.0.1:1/v1", "EMPTY", 1))


class _FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestLoadedModelParsing(unittest.TestCase):
    """Разбор ответа /v1/models: имя модели и её контекст."""

    def _query(self, payload):
        with patch.object(st.urllib.request, "urlopen",
                          return_value=_FakeResponse(payload)):
            return st.query_loaded_model("http://x/v1", "EMPTY")

    def test_loaded_model_with_context(self):
        self.assertEqual(
            self._query({"data": [{"id": "unsloth-ud", "status": {"value": "loaded"},
                                   "meta": {"n_ctx": 262144}}]}),
            ("unsloth-ud", 262144),
        )

    def test_skips_models_that_are_not_loaded(self):
        payload = {"data": [
            {"id": "sleeping", "status": {"value": "not loaded"}, "meta": {"n_ctx": 8}},
            {"id": "awake", "status": {"value": "loaded"}, "meta": {"n_ctx": 131072}},
        ]}
        self.assertEqual(self._query(payload), ("awake", 131072))

    def test_status_as_plain_string_is_accepted(self):
        self.assertEqual(
            self._query({"data": [{"id": "m", "status": "loaded", "n_ctx": 4096}]}),
            ("m", 4096),
        )

    def test_missing_status_takes_the_only_entry(self):
        self.assertEqual(
            self._query({"data": [{"id": "m", "meta": {"n_ctx": 2048}}]}),
            ("m", 2048),
        )

    def test_empty_list_means_no_loaded_model(self):
        self.assertIsNone(self._query({"data": []}))

    def test_missing_n_ctx_returns_zero_context(self):
        # Контекст тогда берётся из .env — шаг 3 просто не трогает MAX_CONTEXT_TOKENS.
        self.assertEqual(
            self._query({"data": [{"id": "m", "status": {"value": "loaded"}}]}),
            ("m", 0),
        )


class TestEnvRewriting(unittest.TestCase):
    """Шаг 3: значения подставляются, комментарии и порядок строк остаются."""

    BASE = (
        "# team_loop\n"
        "OPENAI_BASE_URL=http://localhost:8080/v1\n"
        "OPENAI_API_KEY=EMPTY\n"
        "MODEL_NAME=old-name\n"
        "# MAX_CONTEXT_TOKENS=32768\n"
        "MAX_STEP_ATTEMPTS=3\n"
    )

    def test_replaces_existing_values_in_place(self):
        result = st.apply_env_settings(
            self.BASE, {"MODEL_NAME": "swift-1.5", "MAX_CONTEXT_TOKENS": "131072"}
        )
        self.assertIn("MODEL_NAME=swift-1.5\n", result)
        self.assertNotIn("old-name", result)

    def test_keeps_comments_and_other_settings(self):
        result = st.apply_env_settings(self.BASE, {"MODEL_NAME": "swift-1.5"})
        self.assertIn("# team_loop", result)
        self.assertIn("OPENAI_BASE_URL=http://localhost:8080/v1", result)
        self.assertIn("MAX_STEP_ATTEMPTS=3", result)
        self.assertIn("# MAX_CONTEXT_TOKENS=32768", result)

    def test_commented_out_key_is_not_treated_as_a_setting(self):
        # Закомментированная строка остаётся комментарием, значение дописывается.
        result = st.apply_env_settings(self.BASE, {"MAX_CONTEXT_TOKENS": "131072"})
        self.assertIn("# MAX_CONTEXT_TOKENS=32768", result)
        self.assertIn("MAX_CONTEXT_TOKENS=131072", result)

    def test_inline_comment_survives_the_rewrite(self):
        text = "MAX_CONTEXT_TOKENS=32768   # окно модели\n"
        result = st.apply_env_settings(text, {"MAX_CONTEXT_TOKENS": "131072"})
        self.assertIn("MAX_CONTEXT_TOKENS=131072   # окно модели", result)

    def test_appends_missing_keys(self):
        result = st.apply_env_settings("OPENAI_API_KEY=EMPTY\n",
                                       {"MODEL_NAME": "m", "MAX_CONTEXT_TOKENS": "7"})
        self.assertIn("MODEL_NAME=m", result)
        self.assertIn("MAX_CONTEXT_TOKENS=7", result)

    def test_export_prefix_is_replaced_by_a_plain_assignment(self):
        result = st.apply_env_settings("export MODEL_NAME=old\n",
                                       {"MODEL_NAME": "new"})
        self.assertEqual(result, "MODEL_NAME=new\n")

    def test_crlf_file_stays_crlf(self):
        result = st.apply_env_settings("MODEL_NAME=old\r\n", {"MODEL_NAME": "new"},
                                       newline="\r\n")
        self.assertEqual(result, "MODEL_NAME=new\r\n")

    def test_write_env_settings_writes_utf8_without_bom(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_text("MODEL_NAME=старое-имя\n", encoding="utf-8")

            self.assertTrue(st.write_env_settings(env, {"MODEL_NAME": "новое-имя"}))

            raw = env.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
            self.assertIn("MODEL_NAME=новое-имя", raw.decode("utf-8"))

    def test_cyrillic_utf8_file_is_not_mistaken_for_utf16(self):
        # Произвольный UTF-8 с кириллицей почти всегда декодируется как UTF-16
        # без ошибки: такая каша вернулась бы в .env и убила бы все настройки.
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_text("# окно модели\nMODEL_NAME=старое-имя\n", encoding="utf-8")

            text = st.read_env_text(env)

            self.assertIn("# окно модели", text)
            self.assertIn("MODEL_NAME=старое-имя", text)

    def test_utf16_file_with_bom_is_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_bytes("MODEL_NAME=old\n".encode("utf-16"))

            self.assertIn("MODEL_NAME=old", st.read_env_text(env))

    def test_undecodable_file_is_refused_not_rewritten(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / ".env"
            env.write_bytes("MODEL_NAME=модель\n".encode("cp1251"))

            self.assertIsNone(st.read_env_text(env))
            self.assertFalse(st.write_env_settings(env, {"MODEL_NAME": "m"}))
            # Файл остался как был: плохой .env не портится ещё сильнее.
            self.assertEqual(env.read_bytes(), "MODEL_NAME=модель\n".encode("cp1251"))

    def test_write_env_settings_creates_missing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / "sub" / ".env"

            self.assertFalse(st.write_env_settings(env, {"MODEL_NAME": "m"}))


class TestHandoffCleanup(unittest.TestCase):
    """Шаг 4: новая задача убирает handoff, продолжение старой — оставляет."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.primary = self.root / "handoff.md"
        self.backup = self.root / "handoff.bak"

    def test_deletes_both_state_files(self):
        self.primary.write_text(VALID_HANDOFF, encoding="utf-8")
        self.backup.write_text(VALID_HANDOFF, encoding="utf-8")

        deleted, failed = st.delete_handoff_files([self.primary, self.backup])

        self.assertEqual(deleted, [self.primary, self.backup])
        self.assertEqual(failed, [])
        self.assertFalse(self.primary.exists())
        self.assertFalse(self.backup.exists())

    def test_missing_files_are_not_an_error(self):
        deleted, failed = st.delete_handoff_files([self.primary, self.backup])
        self.assertEqual((deleted, failed), ([], []))

    def test_usable_handoff_is_found_in_backup(self):
        self.backup.write_text(VALID_HANDOFF, encoding="utf-8")

        found = st.has_usable_handoff([self.primary, self.backup])

        self.assertEqual(found, self.backup)

    def test_garbage_handoff_is_not_usable(self):
        self.primary.write_text("не handoff", encoding="utf-8")
        self.backup.write_text("", encoding="utf-8")

        self.assertIsNone(st.has_usable_handoff([self.primary, self.backup]))


class TestAnswers(unittest.TestCase):
    """Ответы Y/N понимаются и по-русски, а спорная «н» трамбуется в «нет»."""

    def test_yes_variants(self):
        for raw, expected in [("y", True), ("Y", True), ("д", True), ("да", True),
                              ("n", False), ("нет", False), ("т", False),
                              ("-", False)]:
            with patch("builtins.input", return_value=raw):
                self.assertIs(st.ask_yes_no("?"), expected, msg=raw)

    def test_bare_cyrillic_n_is_always_no(self):
        # Одна буква «н» опасна: на ЙЦУКЕН под ней клавиша Y, но по-русски это
        # начало «нет». Ошибка стоила бы удалённого handoff.md, поэтому трамбуем
        # её в «нет», а «да» надо писать целиком.
        with patch("builtins.input", return_value="н"):
            self.assertIs(st.ask_yes_no("?"), False)

    def test_empty_answer_uses_default(self):
        with patch("builtins.input", return_value="  "):
            self.assertIs(st.ask_yes_no("?", default=False), False)
            self.assertIs(st.ask_yes_no("?", default=True), True)

    def test_garbage_is_asked_again_and_then_accepted(self):
        answers = ["что-то не то", "", "y"]
        with patch("builtins.input", side_effect=answers):
            self.assertIs(st.ask_yes_no("?"), True)

    def test_ask_text_falls_back_to_default(self):
        with patch("builtins.input", return_value="   "):
            self.assertEqual(st.ask_text("?", "Продолжи выполнение задачи"),
                             "Продолжи выполнение задачи")


class TestRunCommand(unittest.TestCase):
    """Команда цикла: новая задача без --resume, продолжение — с ним."""

    def test_new_task_command(self):
        with patch.object(st, "ask_text", return_value="Собери calc.py"):
            command, text = st.step_build_command(new_task=True)

        self.assertEqual(text, "Собери calc.py")
        self.assertEqual(command[-2:], ["run_strata_team.py", "Собери calc.py"])
        self.assertNotIn("--resume", command)

    def test_resume_command_uses_the_default_prompt(self):
        with patch.object(st, "ask_text",
                          side_effect=lambda prompt, default="": default):
            command, text = st.step_build_command(new_task=False)

        self.assertEqual(text, st.DEFAULT_RESUME_PROMPT)
        self.assertEqual(command[-3:],
                         ["run_strata_team.py", "--resume", st.DEFAULT_RESUME_PROMPT])


if __name__ == "__main__":
    unittest.main()
