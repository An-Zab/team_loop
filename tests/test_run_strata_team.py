"""Регрессионные тесты точки входа и восстановления Handoff (ШАГ 6)."""

from contextlib import redirect_stdout
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import handoff_manager
import run_strata_team as entry


VALID_HANDOFF = (
    "# Исходная задача\n"
    "Создать проверяемый модуль.\n\n"
    "# Статус плана\n"
    "- [ ] Шаг 1. Создать модуль\n"
    "\n# Артефакты и файлы на диске\n"
    "Файлы пока не созданы.\n\n"
    "# Текущий контекст и блокер\n"
    "Продолжить с первого шага.\n"
)


class TestResumeFallback(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.primary = self.root / "handoff.md"
        self.backup = self.root / "handoff.bak"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_empty_primary_uses_backup_without_overwriting_it(self):
        self.primary.write_text("", encoding="utf-8")
        self.backup.write_text(VALID_HANDOFF, encoding="utf-8")

        with patch.object(handoff_manager.team_config, "HANDOFF_FILE", self.primary), \
                patch.object(handoff_manager.team_config, "HANDOFF_BACKUP", self.backup):
            resumed = handoff_manager.load_handoff_if_exists(include_backup=True)

        self.assertEqual(resumed, VALID_HANDOFF)
        self.assertEqual(self.primary.read_text(encoding="utf-8"), "")
        self.assertEqual(self.backup.read_text(encoding="utf-8"), VALID_HANDOFF)

    def test_malformed_primary_uses_backup_without_overwriting_it(self):
        self.primary.write_text("это текст, но не handoff", encoding="utf-8")
        self.backup.write_text(VALID_HANDOFF, encoding="utf-8")

        with patch.object(handoff_manager.team_config, "HANDOFF_FILE", self.primary), \
                patch.object(handoff_manager.team_config, "HANDOFF_BACKUP", self.backup):
            resumed = handoff_manager.load_handoff_if_exists(include_backup=True)

        self.assertEqual(resumed, VALID_HANDOFF)
        self.assertEqual(self.primary.read_text(encoding="utf-8"), "это текст, но не handoff")
        self.assertEqual(self.backup.read_text(encoding="utf-8"), VALID_HANDOFF)

    def test_unreadable_primary_uses_backup_without_overwriting_it(self):
        self.primary.write_text("unreadable primary", encoding="utf-8")
        self.backup.write_text(VALID_HANDOFF, encoding="utf-8")
        original_reader = handoff_manager._read_handoff_text

        def read_with_primary_error(path):
            if path == self.primary:
                return None
            return original_reader(path)

        with patch.object(handoff_manager.team_config, "HANDOFF_FILE", self.primary), \
                patch.object(handoff_manager.team_config, "HANDOFF_BACKUP", self.backup), \
                patch.object(handoff_manager, "_read_handoff_text", side_effect=read_with_primary_error):
            resumed = handoff_manager.load_handoff_if_exists(include_backup=True)

        self.assertEqual(resumed, VALID_HANDOFF)
        self.assertEqual(self.primary.read_text(encoding="utf-8"), "unreadable primary")
        self.assertEqual(self.backup.read_text(encoding="utf-8"), VALID_HANDOFF)

    def test_normal_launch_does_not_consume_backup_when_primary_is_empty(self):
        self.primary.write_text("", encoding="utf-8")
        self.backup.write_text(VALID_HANDOFF, encoding="utf-8")

        with patch.object(handoff_manager.team_config, "HANDOFF_FILE", self.primary), \
                patch.object(handoff_manager.team_config, "HANDOFF_BACKUP", self.backup):
            resumed = handoff_manager.load_handoff_if_exists(include_backup=False)

        self.assertIsNone(resumed)
        self.assertEqual(self.backup.read_text(encoding="utf-8"), VALID_HANDOFF)

    def test_resume_preflight_accepts_backup_when_primary_is_empty(self):
        self.primary.write_text("   \n", encoding="utf-8")
        self.backup.write_text(VALID_HANDOFF, encoding="utf-8")
        output = io.StringIO()

        with redirect_stdout(output):
            error = entry._check_state(True, self.primary, self.backup)

        self.assertEqual(error, "")
        self.assertIn(str(self.backup), output.getvalue())

    def test_resume_preflight_accepts_backup_when_primary_is_malformed(self):
        self.primary.write_text("not a handoff document", encoding="utf-8")
        self.backup.write_text(VALID_HANDOFF, encoding="utf-8")
        output = io.StringIO()

        with redirect_stdout(output):
            error = entry._check_state(True, self.primary, self.backup)

        self.assertEqual(error, "")
        self.assertIn(str(self.backup), output.getvalue())

    def test_resume_preflight_rejects_only_empty_files(self):
        self.primary.write_text(" \n", encoding="utf-8")
        self.backup.write_text("", encoding="utf-8")

        error = entry._check_state(True, self.primary, self.backup)

        self.assertIn("непригодны", error)


class TestStressHandoffValidation(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.handoff_path = Path(self.temp_dir.name) / "handoff.md"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_only_saved_handoff_after_first_accepted_step_passes(self):
        self.handoff_path.write_text(VALID_HANDOFF, encoding="utf-8")
        result = {
            "status": "context_handoff",
            "completed_steps": 1,
            "total_steps": 2,
            "stopped_at_step": 2,
            "handoff_saved": True,
            "handoff_path": str(self.handoff_path),
        }

        passed, reason = entry._validate_test_handoff_result(result, self.handoff_path)

        self.assertTrue(passed)
        self.assertEqual(reason, "")

    def test_completed_without_handoff_is_failure(self):
        result = {
            "status": "completed",
            "completed_steps": 2,
            "handoff_saved": False,
            "handoff_path": str(self.handoff_path),
        }

        passed, reason = entry._validate_test_handoff_result(result, self.handoff_path)

        self.assertFalse(passed)
        self.assertIn("context_handoff", reason)

    def test_handoff_before_first_step_is_failure(self):
        self.handoff_path.write_text(VALID_HANDOFF, encoding="utf-8")
        result = {
            "status": "context_handoff",
            "completed_steps": 0,
            "handoff_saved": True,
            "handoff_path": str(self.handoff_path),
        }

        passed, reason = entry._validate_test_handoff_result(result, self.handoff_path)

        self.assertFalse(passed)
        self.assertIn("до принятия первого шага", reason)

    def test_handoff_after_more_than_first_step_is_failure(self):
        result = {
            "status": "context_handoff",
            "completed_steps": 2,
            "total_steps": 3,
            "stopped_at_step": 3,
            "handoff_saved": True,
            "handoff_path": str(self.handoff_path),
        }

        passed, reason = entry._validate_test_handoff_result(result, self.handoff_path)

        self.assertFalse(passed)
        self.assertIn("не сразу после первого", reason)

    def test_attempts_exhausted_guidance_moves_remaining_handoff(self):
        self.handoff_path.write_text(VALID_HANDOFF, encoding="utf-8")
        result = {
            "status": "attempts_exhausted",
            "completed_steps": 0,
            "total_steps": 1,
            "stopped_at_step": 1,
            "handoff_saved": True,
            "handoff_path": str(self.handoff_path),
            "tokens": 100,
        }
        output = io.StringIO()

        with redirect_stdout(output):
            code = entry._print_summary(result)

        self.assertEqual(code, entry.EXIT_FAILED)
        self.assertIn("Move-Item", output.getvalue())
        self.assertIn(str(self.handoff_path), output.getvalue())

    def test_attempts_exhausted_detects_leftover_when_save_failed(self):
        self.handoff_path.write_text(VALID_HANDOFF, encoding="utf-8")
        result = {
            "status": "attempts_exhausted",
            "completed_steps": 0,
            "total_steps": 1,
            "stopped_at_step": 1,
            "handoff_saved": False,
            "handoff_path": str(self.handoff_path),
            "tokens": 100,
        }
        output = io.StringIO()

        with redirect_stdout(output):
            entry._print_summary(result)

        self.assertIn("Move-Item", output.getvalue())
        self.assertIn("может продолжить старый план", output.getvalue())


if __name__ == "__main__":
    unittest.main()
