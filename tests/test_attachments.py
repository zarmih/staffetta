"""Тесты вложений: pack --attach (одна запись в манифесте + байты в attachments/) и
restore-missing (целостность до записи, восстанавливает только отсутствующее,
существующие и изменённые файлы не трогает, старые передачи без вложений работают)."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

F1 = "src/parse_csv.py"
F2 = "tests/test_parse_csv.py"
F3 = "data/sales_local.csv"

NOTE = (
    "--goal", "починить разбор CSV в импорте продаж",
    "--decision", "чинить парсер, а не данные",
    "--next", "запустить тест разбора на этой машине",
    "--rejected", "править CSV вручную — файл перезаписывается",
    "--checked", "на Mac тест падал с ValueError",
    "--unknown", "поведение на Windows не проверялось",
)


def run_cli(args, cwd):
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "staffetta", *args],
        cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env,
    )


def sha_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class StaffettaAttachmentsTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.sender = self.base / "sender-work"
        self.recipient = self.base / "recipient-work"
        for d in ("src", "tests", "data"):
            (self.sender / d).mkdir(parents=True)
        (self.sender / F1).write_text(
            "def parse_sales(path):\n    raise NotImplementedError\n", encoding="utf-8")
        (self.sender / F2).write_text(
            "def test_parse_sales():\n    assert True\n", encoding="utf-8")
        (self.sender / F3).write_text(
            "date;amount\n2026-11-20;1 234,50\n", encoding="utf-8")

    def pack(self, extra=()):
        result = run_cli(["pack", *NOTE, "--out", "handoff", *extra], cwd=self.sender)
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.sender / "handoff"

    def stored(self, handoff: Path, rel: str) -> Path:
        return handoff.joinpath("attachments", *rel.split("/"))


class TestPackAttach(StaffettaAttachmentsTestCase):
    def test_attach_and_file_give_one_entry_with_bytes(self):
        handoff = self.pack(["--file", F1, "--file", F3, "--attach", F3])
        manifest = json.loads((handoff / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual([e["path"] for e in manifest["files"]], [F1, F3])
        entry = manifest["files"][1]
        self.assertIs(entry["attached"], True)
        self.assertNotIn("attached", manifest["files"][0])
        stored = self.stored(handoff, F3)
        self.assertTrue(stored.is_file())
        self.assertEqual(stored.read_bytes(), (self.sender / F3).read_bytes())
        text = (handoff / "HANDOFF.md").read_text(encoding="utf-8")
        self.assertIn("attachments/", text)
        self.assertIn("restore-missing", text)

    def test_attach_without_file_flag_adds_entry(self):
        handoff = self.pack(["--attach", F3])
        manifest = json.loads((handoff / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual([e["path"] for e in manifest["files"]], [F3])
        self.assertIs(manifest["files"][0]["attached"], True)
        self.assertTrue(self.stored(handoff, F3).is_file())

    def test_force_pack_replaces_stale_attachment_bytes(self):
        handoff = self.pack(["--attach", F3])
        (self.sender / F3).write_text("date;amount\n2026-12-01;9,99\n", encoding="utf-8")
        handoff = self.pack(["--force", "--attach", F3])
        stored = self.stored(handoff, F3)
        self.assertEqual(stored.read_bytes(), (self.sender / F3).read_bytes())
        manifest = json.loads((handoff / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["files"][0]["sha256"], sha_of(self.sender / F3))


class TestRestoreMissing(StaffettaAttachmentsTestCase):
    def prepare_recipient(self):
        """Получатель: F1 совпадает, F2 изменён (локальная правка), F3 отсутствует."""
        (self.recipient / "src").mkdir(parents=True)
        (self.recipient / "tests").mkdir(parents=True)
        (self.recipient / F1).write_text((self.sender / F1).read_text(encoding="utf-8"),
                                         encoding="utf-8")
        (self.recipient / F2).write_text((self.sender / F2).read_text(encoding="utf-8") +
                                         "\n# локальная правка получателя\n",
                                         encoding="utf-8")

    def test_restore_restores_only_missing_and_check_updates(self):
        self.pack(["--file", F1, "--file", F2, "--file", F3,
                   "--attach", F1, "--attach", F2, "--attach", F3])
        self.prepare_recipient()
        before = run_cli(["check", "handoff", str(self.recipient)], cwd=self.sender)
        self.assertEqual(before.returncode, 1)
        for path, status in ((F1, "совпадает"), (F2, "изменён"), (F3, "отсутствует")):
            lines = [ln for ln in before.stdout.splitlines() if path in ln]
            self.assertTrue(lines and status in lines[0], before.stdout)

        h1, h2 = sha_of(self.recipient / F1), sha_of(self.recipient / F2)
        result = run_cli(["restore-missing", "handoff", str(self.recipient)], cwd=self.sender)
        self.assertEqual(result.returncode, 0, result.stderr)
        for path, label in ((F1, "уже на месте — не тронут"),
                            (F2, "изменён — не тронут"),
                            (F3, "восстановлен")):
            lines = [ln for ln in result.stdout.splitlines() if path in ln]
            self.assertTrue(lines and label in lines[0], result.stdout)
        self.assertIn("1 восстановлено, 1 уже на месте, 1 изменённых не тронуто", result.stdout)
        # Восстановлено ровно байтами отправителя; изменённый и совпадающий не тронуты.
        self.assertEqual((self.recipient / F3).read_bytes(), (self.sender / F3).read_bytes())
        self.assertEqual(sha_of(self.recipient / F2), h2)
        self.assertEqual(sha_of(self.recipient / F1), h1)

        after = run_cli(["check", "handoff", str(self.recipient)], cwd=self.sender)
        self.assertEqual(after.returncode, 1, "изменённый F2 должен держать check на 1")
        for path, status in ((F1, "совпадает"), (F2, "изменён"), (F3, "совпадает")):
            lines = [ln for ln in after.stdout.splitlines() if path in ln]
            self.assertTrue(lines and status in lines[0], after.stdout)

    def test_restore_reports_corrupted_attachment_and_writes_nothing(self):
        handoff = self.pack(["--attach", F3])
        self.stored(handoff, F3).write_bytes(b"tampered")
        self.recipient.mkdir()

        result = run_cli(["restore-missing", str(handoff), str(self.recipient)],
                         cwd=self.sender)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("повреждено", result.stderr)
        self.assertIn(F3, result.stderr)
        self.assertIn("ни один файл не записан", result.stderr)
        self.assertFalse((self.recipient / F3).exists(), "частично записанный файл")

    def test_restore_validates_all_attachments_before_first_write(self):
        handoff = self.pack(["--attach", F2, "--attach", F3])
        self.stored(handoff, F2).write_bytes(b"tampered")
        self.recipient.mkdir()  # отсутствуют и F2, и F3

        result = run_cli(["restore-missing", str(handoff), str(self.recipient)],
                         cwd=self.sender)
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.recipient / F2).exists(), "частичное восстановление F2")
        self.assertFalse((self.recipient / F3).exists(),
                         "F3 записан, хотя вложение F2 повреждено — "
                         "валидация должна идти до первой записи")

    def test_restore_with_old_manifest_without_attachments(self):
        handoff = self.pack(["--file", F1, "--file", F2, "--file", F3])
        manifest = json.loads((handoff / "manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(all("attached" not in e for e in manifest["files"]))
        self.recipient.mkdir()
        (self.recipient / F1).parent.mkdir(parents=True, exist_ok=True)
        (self.recipient / F1).write_text((self.sender / F1).read_text(encoding="utf-8"),
                                         encoding="utf-8")

        result = run_cli(["restore-missing", str(handoff), str(self.recipient)],
                         cwd=self.sender)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("нет вложений", result.stdout)
        self.assertTrue((self.recipient / F1).is_file())

    def test_restore_rejects_manifest_path_escape(self):
        handoff = self.sender / "handoff"
        handoff.mkdir()
        manifest = {
            "format": "staffetta-manifest/1",
            "note": {},
            "files": [{"path": "../evil.txt", "sha256": "0" * 64,
                       "size": 6, "attached": True}],
        }
        (handoff / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.recipient.mkdir()
        result = run_cli(["restore-missing", str(handoff), str(self.recipient)],
                         cwd=self.sender)
        self.assertEqual(result.returncode, 2)
        self.assertIn("недопустимый путь", result.stderr)
        self.assertFalse((self.base / "evil.txt").exists())


if __name__ == "__main__":
    unittest.main()
