"""Сквозной тест сценария из задания: pack у отправителя (3 файла), check у получателя
(первый файл тот же, второй изменён, третий отсутствует), три статуса, только чтение."""
import hashlib
import json
import os
import shutil
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


def snapshot(root: Path):
    """Перечень (путь, признак каталога, размер, mtime_ns) — чтобы убедиться,
    что check ничего не меняет в рабочей копии."""
    items = []
    for p in sorted(root.rglob("*")):
        st = p.stat()
        items.append((p.relative_to(root).as_posix(), p.is_dir(), st.st_size, st.st_mtime_ns))
    return items


class PackCheckScenario(unittest.TestCase):
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
        return run_cli(["pack", *NOTE, "--out", "handoff", *extra], cwd=self.sender)

    def assert_status(self, output, path, status):
        lines = [ln for ln in output.splitlines() if path in ln]
        self.assertTrue(lines, f"нет строки для {path} в выводе:\n{output}")
        self.assertIn(status, lines[0], f"не тот статус для {path}:\n{output}")

    def test_pack_creates_handoff_and_manifest(self):
        result = self.pack(["--file", F1, "--file", F2, "--file", F3])
        self.assertEqual(result.returncode, 0, result.stderr)
        handoff = self.sender / "handoff"
        self.assertTrue((handoff / "HANDOFF.md").is_file())
        manifest = json.loads((handoff / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual([e["path"] for e in manifest["files"]], [F1, F2, F3])
        entry3 = next(e for e in manifest["files"] if e["path"] == F3)
        real = hashlib.sha256((self.sender / F3).read_bytes()).hexdigest()
        self.assertEqual(entry3["sha256"], real)
        self.assertEqual(entry3["size"], (self.sender / F3).stat().st_size)
        text = (handoff / "HANDOFF.md").read_text(encoding="utf-8")
        for needle in (
            "починить разбор CSV", "чинить парсер, а не данные", "запустить тест разбора",
            "править CSV вручную", "manifest.json", "совпадает / изменён / отсутствует",
        ):
            self.assertIn(needle, text)

    def test_check_distinguishes_match_changed_missing(self):
        self.assertEqual(self.pack(["--file", F1, "--file", F2, "--file", F3]).returncode, 0)
        # Получатель: первый файл оставлен как есть, второй изменён, третий отсутствует.
        (self.recipient / "src").mkdir(parents=True)
        (self.recipient / "tests").mkdir(parents=True)
        shutil.copy(self.sender / F1, self.recipient / F1)
        shutil.copy(self.sender / F2, self.recipient / F2)
        with (self.recipient / F2).open("a", encoding="utf-8") as fh:
            fh.write("\n# локальная правка получателя\n")

        before = snapshot(self.recipient)
        result = run_cli(["check", "handoff", str(self.recipient)], cwd=self.sender)
        after = snapshot(self.recipient)

        self.assertEqual(before, after, "check изменил рабочую копию")
        self.assertEqual(result.returncode, 1, "не всё совпадает — ожидался код 1")
        self.assert_status(result.stdout, F1, "совпадает")
        self.assert_status(result.stdout, F2, "изменён")
        self.assert_status(result.stdout, F3, "отсутствует")

    def test_check_all_match_returns_zero(self):
        self.assertEqual(self.pack(["--file", F1, "--file", F2, "--file", F3]).returncode, 0)
        shutil.copytree(self.sender, self.recipient,
                        ignore=shutil.ignore_patterns("handoff"))
        result = run_cli(["check", "handoff", str(self.recipient)], cwd=self.sender)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("3 совпадает", result.stdout)

    def test_pack_missing_file_fails(self):
        result = self.pack(["--file", F1, "--file", "nope.txt"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("nope.txt", result.stderr)

    def test_pack_refuses_existing_out_without_force(self):
        self.assertEqual(self.pack(["--file", F1]).returncode, 0)
        again = self.pack(["--file", F1])
        self.assertNotEqual(again.returncode, 0)
        forced = self.pack(["--force", "--file", F1])
        self.assertEqual(forced.returncode, 0, forced.stderr)

    def test_pack_rejects_file_outside_root(self):
        (self.base / "outside.txt").write_text("секрет", encoding="utf-8")
        result = self.pack(["--file", "../outside.txt"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("вне корня", result.stderr)

    def test_check_rejects_manifest_escaping_workdir(self):
        handoff = self.sender / "handoff"
        handoff.mkdir()
        manifest = {
            "format": "staffetta-manifest/1",
            "note": {},
            "files": [{"path": "../outside.txt", "sha256": "0" * 64, "size": 6}],
        }
        (handoff / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.recipient.mkdir()
        result = run_cli(["check", str(handoff), str(self.recipient)], cwd=self.sender)
        self.assertEqual(result.returncode, 2)
        self.assertIn("недопустимый путь", result.stderr)


if __name__ == "__main__":
    unittest.main()
