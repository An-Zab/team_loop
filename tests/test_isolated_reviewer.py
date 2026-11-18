"""Регрессии блокеров Ревьюера; локальная модель для тестов не нужна."""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import isolated_reviewer as reviewer


class TestReviewPromptLimits(unittest.TestCase):
    def test_short_prompt_and_isolation_are_unchanged(self):
        messages = reviewer._build_review_messages("Цель", "Отчёт", "Проверка")
        self.assertEqual(messages, [
            {"role": "system", "content": reviewer.REVIEWER_SYSTEM_PROMPT},
            {"role": "user", "content": (
                "Цель текущего шага:\nЦель\n\n"
                "Отчет/код исполнителя:\nОтчёт\n\n"
                "Результат объективной проверки файлов:\nПроверка"
            )},
        ])

    def test_empty_inputs_still_have_placeholders(self):
        user_text = reviewer._build_review_messages("", "", "OK")[1]["content"]
        self.assertIn("(цель не передана)", user_text)
        self.assertIn("(отчет не передан)", user_text)

    def test_each_prompt_block_is_bounded(self):
        labels = (
            "Цель текущего шага:\n",
            "Отчет/код исполнителя:\n",
            "Результат объективной проверки файлов:\n",
        )
        for index in range(3):
            with self.subTest(block=labels[index]):
                blocks = ["goal", "report", "checks"]
                blocks[index] = "я" * 300_000
                text = reviewer._build_review_messages(*blocks)[1]["content"]
                self.assertLess(len(text), 24_000)
                self.assertIn("текст обрезан", text)
                for label in labels:
                    self.assertIn(label, text)

    def test_all_oversized_blocks_together_are_bounded(self):
        messages = reviewer._build_review_messages(*(["я" * 300_000] * 3))
        self.assertLess(sum(len(message["content"]) for message in messages), 24_000)
        self.assertEqual(messages[1]["content"].count("текст обрезан"), 3)

    def test_long_report_keeps_the_beginning_and_final_results(self):
        report = "НАЧАЛО ОТЧЁТА\n" + "код\n" * 100_000 + "\nИТОГ: тесты пройдены"
        text = reviewer._build_review_messages("Цель", report, "OK")[1]["content"]
        self.assertLess(len(text), 24_000)
        self.assertIn("НАЧАЛО ОТЧЁТА", text)
        self.assertIn("ИТОГ: тесты пройдены", text)
        self.assertIn("текст обрезан", text)

    def test_large_report_passes_through_public_review_without_overflow(self):
        captured = []

        def context_limited_model(messages):
            captured.append(messages)
            # Имитируем отказ сервера на слишком большой stateless-запрос.
            if sum(len(message["content"]) for message in messages) > 24_000:
                return None, reviewer.MODEL_ERROR_PREFIX + "HTTP 400: context overflow"
            return {"choices": [{"message": {"content": "ПРИНЯТО\nПроверено"}}],
                    "usage": {"total_tokens": 123}}, None

        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "valid.py"
            path.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
            with patch.object(reviewer, "_call_local_model", side_effect=context_limited_model):
                result = reviewer.review_step(
                    "Проверить модуль. " * 20_000,
                    "def add(a, b):\n    return a + b\n" * 20_000,
                    [str(path)],
                )
        self.assertTrue(result["accepted"])
        self.assertEqual(result["review_tokens"], 123)
        self.assertEqual(len(captured), 1)
        self.assertEqual([message["role"] for message in captured[0]], ["system", "user"])
        self.assertIn(reviewer.OBJECTIVE_OK_MESSAGE, captured[0][1]["content"])

    def test_syntax_error_still_rejects_without_calling_model(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "broken.py"
            path.write_text("def broken(:\n    pass\n", encoding="utf-8")
            with patch.object(reviewer, "_call_local_model") as model:
                result = reviewer.review_step("Создать модуль", "Отчёт" * 100_000, [str(path)])
        model.assert_not_called()
        self.assertFalse(result["accepted"])
        self.assertEqual(result["review_tokens"], 0)
        self.assertIn("Ошибка компиляции Python", result["feedback"])


if __name__ == "__main__":
    unittest.main()
