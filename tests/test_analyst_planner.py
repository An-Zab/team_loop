"""Регрессии сохранности handoff и отказоустойчивости Аналитика без сети."""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import analyst_planner as planner
import team_config


HANDOFF = (
    "# Исходная задача\nЗавершить проект\n\n"
    "# Статус плана\n- [x] Создать модуль\n- [ ] Проверить модуль\n\n"
    "# Артефакты и файлы на диске\nmodule.py\n\n"
    "# Текущий контекст и блокер\nНужна проверка\n"
)


def response_with(answer):
    return {"choices": [{"message": {"content": answer}}],
            "usage": {"total_tokens": 77}}


class TestHandoffPlanTransaction(unittest.TestCase):
    def setUp(self):
        root = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(root) / "handoff.md"
        self.backup = Path(root) / "handoff.bak"
        self.enterContext(patch.object(team_config, "HANDOFF_FILE", self.path))
        self.enterContext(patch.object(team_config, "HANDOFF_BACKUP", self.backup))
        self.warn = self.enterContext(patch.object(planner, "_warn"))
        self.model = self.enterContext(patch.object(
            planner, "_call_local_model",
            return_value=(response_with('["Проверить модуль"]'), None),
        ))
        self.path.write_text(HANDOFF, encoding="utf-8")

    def assert_handoff_preserved(self):
        self.assertTrue(self.path.is_file(), "Следующий запуск должен найти handoff.md")
        self.assertEqual(self.path.read_text(encoding="utf-8"), HANDOFF)
        self.assertEqual(self.backup.read_text(encoding="utf-8"), HANDOFF)

    def test_success_consumes_handoff_and_returns_model_tokens(self):
        self.backup.write_text("Старый handoff", encoding="utf-8")
        steps, handoff, tokens = planner.build_execution_plan()
        self.assertEqual((steps, handoff, tokens), (["Проверить модуль"], HANDOFF, 77))
        self.assertFalse(self.path.exists())
        self.assertEqual(self.backup.read_text(encoding="utf-8"), HANDOFF)
        messages = self.model.call_args.args[0]
        self.assertIn(planner.HANDOFF_ANALYSIS_INSTRUCTION, messages[1]["content"])

    def test_completed_handoff_is_also_a_successful_plan(self):
        self.model.return_value = response_with("[]"), None
        steps, handoff, tokens = planner.build_execution_plan()
        self.assertEqual(len(steps), 1)
        self.assertIn("Сверь результат", steps[0])
        self.assertEqual((handoff, tokens), (HANDOFF, 77))
        self.assertFalse(self.path.exists())
        self.assertEqual(self.backup.read_text(encoding="utf-8"), HANDOFF)

    def test_expected_model_failures_restore_handoff(self):
        for answer, error, expected_tokens in [
            (None, planner.MODEL_ERROR_PREFIX + "сервер недоступен", 0),
            ("", None, 77),
            ("Я не могу составить план.", None, 77),
        ]:
            with self.subTest(answer=answer, error=error):
                self.path.write_text(HANDOFF, encoding="utf-8")
                self.model.return_value = response_with(answer), error
                steps, handoff, tokens = planner.build_execution_plan("Завершить проект")
                self.assertEqual((steps, handoff, tokens), (["Завершить проект"], HANDOFF, expected_tokens))
                self.assert_handoff_preserved()

    def test_unexpected_errors_at_each_planning_stage_restore_handoff(self):
        for target, method, expected_tokens in [
            (planner, "_build_plan_messages", 0),
            (planner, "_call_local_model", 0),
            (planner, "_extract_answer_text", 0),
            (team_config, "extract_used_tokens", 0),
            (planner, "_parse_plan_answer", 77),
        ]:
            with self.subTest(stage=method):
                self.path.write_text(HANDOFF, encoding="utf-8")
                with patch.object(target, method, side_effect=RuntimeError("Сбой планирования")):
                    steps, handoff, tokens = planner.build_execution_plan("Завершить проект")
                self.assertEqual((steps, handoff, tokens), (["Завершить проект"], HANDOFF, expected_tokens))
                self.assert_handoff_preserved()
                self.assertTrue(any("RuntimeError" in call.args[0] for call in self.warn.call_args_list))

    def test_interruptions_propagate_after_restoring_handoff(self):
        for method in ("_build_plan_messages", "_call_local_model", "_extract_answer_text", "_parse_plan_answer"):
            for error_type in (KeyboardInterrupt, SystemExit):
                with self.subTest(stage=method, error=error_type.__name__):
                    self.path.write_text(HANDOFF, encoding="utf-8")
                    error = error_type("Остановка пользователем")
                    with patch.object(planner, method, side_effect=error):
                        with self.assertRaises(error_type) as caught:
                            planner.build_execution_plan()
                    self.assertIs(caught.exception, error)
                    self.assert_handoff_preserved()

    def test_exception_in_fallback_also_restores_handoff(self):
        self.model.return_value = None, "Сервер недоступен"
        with patch.object(planner, "_degraded_plan", side_effect=RuntimeError("Сбой запасного плана")):
            with self.assertRaises(RuntimeError):
                planner.build_execution_plan()
        self.assert_handoff_preserved()

    def test_recovery_does_not_overwrite_a_new_handoff(self):
        def interrupt_with_new_handoff(_messages):
            self.path.write_text("Новая точка продолжения", encoding="utf-8")
            raise KeyboardInterrupt

        self.model.side_effect = interrupt_with_new_handoff
        with self.assertRaises(KeyboardInterrupt):
            planner.build_execution_plan()
        self.assertEqual(self.path.read_text(encoding="utf-8"), "Новая точка продолжения")
        self.assertEqual(self.backup.read_text(encoding="utf-8"), HANDOFF)

    def test_restore_io_failure_does_not_mask_keyboard_interrupt(self):
        self.model.side_effect = KeyboardInterrupt
        with patch.object(Path, "write_text", side_effect=PermissionError("Файл занят")):
            with self.assertRaises(KeyboardInterrupt):
                planner.build_execution_plan()
        self.assertEqual(self.backup.read_text(encoding="utf-8"), HANDOFF)
        self.assertTrue(any("не удалось вернуть" in call.args[0] for call in self.warn.call_args_list))

    def test_deep_json_is_a_failure_not_a_completed_handoff(self):
        depth = sys.getrecursionlimit() + 100
        for answer in (
            "[" * depth + '"Проверить модуль"' + "]" * depth,
            '{"nested":' * depth + "null" + "}" * depth,
        ):
            with self.subTest(kind=answer[0]):
                self.path.write_text(HANDOFF, encoding="utf-8")
                self.model.return_value = response_with(answer), None
                steps, handoff, tokens = planner.build_execution_plan("Завершить проект")
                self.assertEqual((steps, handoff, tokens), (["Завершить проект"], HANDOFF, 77))
                self.assert_handoff_preserved()
                self.assertEqual(planner.parse_plan_steps(answer), [])

    def test_next_run_resumes_after_a_failed_plan(self):
        self.model.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            planner.build_execution_plan()
        self.model.side_effect = None
        steps, handoff, tokens = planner.build_execution_plan()
        self.assertEqual((steps, handoff, tokens), (["Проверить модуль"], HANDOFF, 77))
        self.assertFalse(self.path.exists())

    def test_missing_handoff_is_not_created_on_failure(self):
        self.path.unlink()
        self.model.side_effect = RuntimeError("Сбой запроса")
        self.assertEqual(planner.build_execution_plan("Создать модуль"), (["Создать модуль"], None, 0))
        self.assertFalse(self.path.exists())
        self.assertFalse(self.backup.exists())

    def test_no_task_and_no_handoff_skips_model(self):
        self.path.unlink()
        steps, handoff, tokens = planner.build_execution_plan()
        self.assertEqual(len(steps), 1)
        self.assertIn("Уточни", steps[0])
        self.assertIsNone(handoff)
        self.assertEqual(tokens, 0)
        self.model.assert_not_called()


if __name__ == "__main__":
    unittest.main()
