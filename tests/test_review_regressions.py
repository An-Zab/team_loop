"""Регрессионные проверки замечаний ревью без обращения к локальной модели."""

from contextlib import redirect_stdout
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import handoff_manager
import isolated_reviewer
import loop_team
import run_strata_team as entry
import team_config


VALID_HANDOFF = (
    "# Исходная задача\nЗавершить проект\n\n"
    "# Статус плана\n- [ ] Проверить модуль\n\n"
    "# Артефакты и файлы на диске\nmodule.py\n\n"
    "# Текущий контекст и блокер\nПродолжить проверку.\n"
)


def model_response(tokens=100, content="готово"):
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"total_tokens": tokens},
    }


class TestFileToolSafety(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name).resolve()
        self.project_patch = patch.object(loop_team, "_PROJECT_DIR", self.root)
        self.project_patch.start()
        self.addCleanup(self.project_patch.stop)

    def test_truncated_read_cannot_overwrite_and_lose_file_tail(self):
        path = self.root / "large.txt"
        original = "VISIBLE-HEAD\n" + "x" * (loop_team.MAX_TOOL_RESULT_CHARS + 50) + "\nTAIL-MARKER"
        path.write_text(original, encoding="utf-8")
        state = {}

        read_result = loop_team._tool_read_file({"path": "large.txt"}, state)
        write_result = loop_team._tool_write_file(
            {"path": "large.txt", "content": "replacement"}, [],
            file_read_state=state,
        )

        self.assertIn("текст обрезан", read_result)
        self.assertNotIn("TAIL-MARKER", read_result)
        self.assertIn("Перезапись", write_result)
        self.assertIn("усечено", write_result)
        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_full_read_allows_overwrite_of_existing_file(self):
        path = self.root / "small.txt"
        path.write_text("старое содержимое", encoding="utf-8")
        state = {}

        self.assertEqual(
            loop_team._tool_read_file({"path": "small.txt"}, state),
            "старое содержимое",
        )
        result = loop_team._tool_write_file(
            {"path": "small.txt", "content": "новое содержимое"}, [],
            file_read_state=state,
        )

        self.assertIn("Файл записан", result)
        self.assertEqual(path.read_text(encoding="utf-8"), "новое содержимое")

    def test_new_file_does_not_require_a_prior_read(self):
        result = loop_team._tool_write_file(
            {"path": "new.txt", "content": "новый файл"}, []
        )
        self.assertIn("Файл записан", result)
        self.assertEqual((self.root / "new.txt").read_text(encoding="utf-8"), "новый файл")

    def test_invalid_utf8_is_rejected_without_replacement_or_overwrite(self):
        path = self.root / "legacy.txt"
        original = b"before\xffafter"
        path.write_bytes(original)
        state = {}

        read_result = loop_team._tool_read_file({"path": "legacy.txt"}, state)
        write_result = loop_team._tool_write_file(
            {"path": "legacy.txt", "content": "перезаписанный файл"}, [],
            file_read_state=state,
        )

        self.assertIn("как UTF-8", read_result)
        self.assertNotIn("\ufffd", read_result)
        self.assertIn("Перезапись", write_result)
        self.assertEqual(path.read_bytes(), original)


class TestToolCallIdConsistency(unittest.TestCase):
    def test_synthetic_ids_are_written_to_assistant_and_tool_messages(self):
        calls = [
            {"function": {"name": "unknown_a", "arguments": "{}"}},
            {"id": "duplicate", "function": {"name": "unknown_b", "arguments": "{}"}},
            {"id": "duplicate", "function": {"name": "unknown_c", "arguments": "{}"}},
        ]
        first = {
            "choices": [{"message": {"content": "", "tool_calls": calls}}],
            "usage": {"total_tokens": 50},
        }
        final = model_response(60, "Готово")
        messages = []

        with patch.object(loop_team, "_call_local_model", side_effect=[(first, "", None), (final, "", None)]):
            report, response = loop_team._run_executor(messages, "Сделай шаг", [])

        assistant_message = next(
            message for message in messages
            if message.get("role") == "assistant" and "tool_calls" in message
        )
        assistant_ids = [call["id"] for call in assistant_message["tool_calls"]]
        tool_ids = [
            message["tool_call_id"] for message in messages
            if message.get("role") == "tool"
        ]
        self.assertEqual(len(assistant_ids), 3)
        self.assertEqual(len(set(assistant_ids)), 3)
        self.assertEqual(tool_ids, assistant_ids)
        self.assertTrue(all(assistant_ids))
        self.assertEqual(report, "Готово")
        self.assertIs(response, final)


class TestReviewerHeuristic(unittest.TestCase):
    def test_step_without_file_output_is_still_reviewed_by_model(self):
        response = model_response(17, "ПРИНЯТО\nТесты запущены успешно")
        with patch.object(isolated_reviewer, "_call_local_model", return_value=(response, None)) as model:
            result = isolated_reviewer.review_step(
                "Запустить тесты без изменений файлов",
                "Команда выполнена, тесты прошли.",
                [],
            )

        model.assert_called_once()
        self.assertTrue(result["accepted"])
        self.assertEqual(result["review_tokens"], 17)
        self.assertIn("Файлы для объективной проверки не переданы", model.call_args.args[0][1]["content"])


class TestLoopHandoffState(unittest.TestCase):
    def setUp(self):
        self.stdout = io.StringIO()
        self.stdout_context = redirect_stdout(self.stdout)
        self.stdout_context.__enter__()
        self.addCleanup(self.stdout_context.__exit__, None, None, None)

    def _accepted_review(self):
        return {"accepted": True, "feedback": "ПРИНЯТО", "review_tokens": 1}

    def test_test_handoff_keeps_first_step_budget_then_stops_on_next_step(self):
        observed_limits = []

        def execute(messages, step_message, touched_files):
            observed_limits.append(team_config.HANDOFF_TOKEN_LIMIT)
            messages.extend([
                {"role": "user", "content": step_message},
                {"role": "assistant", "content": "Шаг выполнен."},
            ])
            return "Шаг выполнен.", model_response(240)

        with patch.object(team_config, "HANDOFF_TOKEN_LIMIT", 3000), \
                patch.object(loop_team.analyst_planner, "build_execution_plan",
                             return_value=(["Шаг один", "Шаг два"], None, 0)), \
                patch.object(loop_team, "_run_executor", side_effect=execute), \
                patch.object(loop_team.isolated_reviewer, "review_step",
                             return_value=self._accepted_review()), \
                patch.object(loop_team.handoff_manager, "generate_handoff",
                             return_value="сохранено") as handoff:
            result = loop_team.run_team_loop("Задача", test_handoff_limit=800)
            limit_after_run = team_config.HANDOFF_TOKEN_LIMIT

        self.assertEqual(observed_limits, [3000])
        self.assertEqual(result["status"], "context_handoff")
        self.assertEqual(result["completed_steps"], 1)
        self.assertEqual(result["stopped_at_step"], 2)
        self.assertEqual(handoff.call_args.args[2], 1)
        self.assertEqual(handoff.call_args.kwargs["use_model"], True)
        self.assertEqual(limit_after_run, 3000)

    def test_pruning_recalculates_tokens_before_the_next_step(self):
        usage = iter([900, 1300, 500])
        none_call_count = 0
        critical_inputs = []

        def extract(response, messages):
            nonlocal none_call_count
            if response is None:
                none_call_count += 1
                return 10 if none_call_count == 1 else 25
            return response["usage"]["total_tokens"]

        def execute(messages, step_message, touched_files):
            report = f"Отчёт {len(messages)}"
            messages.extend([
                {"role": "user", "content": step_message},
                {"role": "assistant", "content": report},
            ])
            return report, model_response(next(usage))

        reviews = [
            {"accepted": False, "feedback": "Доработай", "review_tokens": 1},
            self._accepted_review(),
            self._accepted_review(),
        ]

        with patch.object(loop_team.analyst_planner, "build_execution_plan",
                          return_value=(["Шаг один", "Шаг два"], None, 0)), \
                patch.object(loop_team, "_run_executor", side_effect=execute), \
                patch.object(loop_team.isolated_reviewer, "review_step", side_effect=reviews), \
                patch.object(team_config, "extract_used_tokens", side_effect=extract), \
                patch.object(team_config, "is_context_critical",
                             side_effect=lambda value: critical_inputs.append(value) or False), \
                patch.object(team_config, "MAX_STEP_ATTEMPTS", 2):
            result = loop_team.run_team_loop("Задача")

        self.assertEqual(result["status"], "completed")
        # Проверки до попыток: шаг 1 — 10 токенов, шаг 2 после pruning — 25,
        # а не устаревшие 1300 токенов от ответа до удаления истории.
        self.assertEqual(critical_inputs[0], 10)
        self.assertEqual(critical_inputs[3], 1300)  # последняя попытка до pruning
        self.assertEqual(critical_inputs[4], 25)  # новый шаг после пересчёта
        self.assertEqual(none_call_count, 2)

    def test_ctrl_c_saves_state_and_marks_already_accepted_steps_complete(self):
        execution_count = 0

        def execute(messages, step_message, touched_files):
            nonlocal execution_count
            execution_count += 1
            messages.append({"role": "user", "content": step_message})
            if execution_count == 2:
                raise KeyboardInterrupt
            messages.append({"role": "assistant", "content": "Первый шаг готов."})
            return "Первый шаг готов.", model_response(100)

        with patch.object(team_config, "HANDOFF_TOKEN_LIMIT", 10000), \
                patch.object(team_config, "is_context_critical", return_value=False), \
                patch.object(loop_team.analyst_planner, "build_execution_plan",
                             return_value=(["Шаг один", "Шаг два"], None, 0)), \
                patch.object(loop_team, "_run_executor", side_effect=execute), \
                patch.object(loop_team.isolated_reviewer, "review_step",
                             return_value=self._accepted_review()), \
                patch.object(loop_team.handoff_manager, "generate_handoff",
                             return_value="сохранено") as handoff:
            result = loop_team.run_team_loop("Задача")

        self.assertEqual(result["status"], "interrupted")
        self.assertTrue(result["handoff_saved"])
        self.assertEqual(result["completed_steps"], 1)
        self.assertEqual(result["stopped_at_step"], 2)
        self.assertEqual(handoff.call_args.args[2], 1)
        self.assertFalse(handoff.call_args.kwargs["use_model"])

    def test_interrupt_while_planning_saves_new_task_without_model_retry(self):
        with patch.object(loop_team.analyst_planner, "build_execution_plan",
                          side_effect=KeyboardInterrupt), \
                patch.object(loop_team.handoff_manager, "generate_handoff",
                             return_value="сохранено") as handoff:
            result = loop_team.run_team_loop("Новая задача")

        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["completed_steps"], 0)
        self.assertTrue(result["handoff_saved"])
        self.assertFalse(handoff.call_args.kwargs["use_model"])

    def test_interrupt_during_plan_preserves_existing_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            primary = Path(directory) / "handoff.md"
            backup = Path(directory) / "handoff.bak"
            primary.write_text(VALID_HANDOFF, encoding="utf-8")
            with patch.object(handoff_manager.team_config, "HANDOFF_FILE", primary), \
                    patch.object(handoff_manager.team_config, "HANDOFF_BACKUP", backup), \
                    patch.object(loop_team.analyst_planner, "build_execution_plan",
                                 side_effect=KeyboardInterrupt), \
                    patch.object(loop_team.handoff_manager, "generate_handoff") as generate:
                result = loop_team.run_team_loop("Новая задача")
                saved_text = primary.read_text(encoding="utf-8")

        generate.assert_not_called()
        self.assertEqual(result["status"], "interrupted")
        self.assertTrue(result["handoff_saved"])
        self.assertEqual(result["handoff_path"], str(primary))
        self.assertEqual(saved_text, VALID_HANDOFF)

    def test_interrupted_summary_returns_shell_interrupt_code(self):
        output = io.StringIO()
        result = {
            "status": "interrupted",
            "completed_steps": 1,
            "total_steps": 2,
            "stopped_at_step": 2,
            "handoff_saved": True,
            "handoff_path": "handoff.md",
            "tokens": 100,
        }
        with redirect_stdout(output):
            code = entry._print_summary(result)

        self.assertEqual(code, entry.EXIT_INTERRUPTED)
        self.assertIn("--resume", output.getvalue())

    def test_completed_handoff_is_archived_after_success(self):
        with patch.object(loop_team.analyst_planner, "build_execution_plan",
                          return_value=(["Проверить итог"], VALID_HANDOFF, 0)), \
                patch.object(loop_team, "_run_executor",
                             return_value=("Результат проверен", model_response(100))), \
                patch.object(loop_team.isolated_reviewer, "review_step",
                             return_value=self._accepted_review()), \
                patch.object(loop_team.handoff_manager, "archive_consumed_handoff",
                             return_value=True) as archive:
            result = loop_team.run_team_loop("")

        self.assertEqual(result["status"], "completed")
        archive.assert_called_once_with(VALID_HANDOFF)


class TestHandoffArchiveRotation(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.primary = root / "handoff.md"
        self.backup = root / "handoff.bak"
        self.previous = root / "handoff.bak.1"
        self.patches = [
            patch.object(handoff_manager.team_config, "HANDOFF_FILE", self.primary),
            patch.object(handoff_manager.team_config, "HANDOFF_BACKUP", self.backup),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_interrupt_handoff_uses_local_fallback_without_model_request(self):
        with patch.object(handoff_manager, "_call_local_model") as model:
            document = handoff_manager.generate_handoff(
                "Задача", ["Шаг один", "Шаг два"], 1, [], "Ctrl+C", use_model=False
            )

        model.assert_not_called()
        self.assertIsNotNone(document)
        self.assertIn("- [x] Шаг 1. Шаг один", document)
        self.assertIn("- [ ] Шаг 2. Шаг два", document)
        self.assertEqual(self.primary.read_text(encoding="utf-8"), document)

    def test_archive_keeps_previous_backup_generation(self):
        old = VALID_HANDOFF.replace("Завершить проект", "Предыдущая задача")
        older = VALID_HANDOFF.replace("Завершить проект", "Более старая задача")
        current = VALID_HANDOFF.replace("Завершить проект", "Текущая задача")
        self.backup.write_text(old, encoding="utf-8")
        self.previous.write_text(older, encoding="utf-8")
        self.primary.write_text(current, encoding="utf-8")

        loaded = handoff_manager.load_handoff_if_exists()

        self.assertEqual(loaded, current)
        self.assertEqual(self.backup.read_text(encoding="utf-8"), current)
        self.assertEqual(self.previous.read_text(encoding="utf-8"), old)
        self.assertFalse(self.primary.exists())

    def test_completed_backup_is_archived_and_not_reused_by_resume(self):
        self.backup.write_text(VALID_HANDOFF, encoding="utf-8")

        archived = handoff_manager.archive_consumed_handoff(VALID_HANDOFF)
        resumed = handoff_manager.load_handoff_if_exists(include_backup=True)

        self.assertTrue(archived)
        self.assertIsNone(resumed)
        self.assertFalse(self.backup.exists())
        self.assertEqual(
            (self.backup.with_name(self.backup.name + ".completed")).read_text(encoding="utf-8"),
            VALID_HANDOFF,
        )

    def test_newer_backup_is_not_archived_by_old_completion(self):
        newer = VALID_HANDOFF.replace("Завершить проект", "Новая задача")
        self.backup.write_text(newer, encoding="utf-8")

        archived = handoff_manager.archive_consumed_handoff(VALID_HANDOFF)

        self.assertFalse(archived)
        self.assertEqual(self.backup.read_text(encoding="utf-8"), newer)


if __name__ == "__main__":
    unittest.main()
