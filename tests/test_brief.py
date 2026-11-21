"""Тесты brief: готовая записка для чата без доступа к диску + жёсткое правило
«при любой ошибке stdout пуст, причина в stderr, код ненулевой»."""
import unittest

from test_attachments import F1, F2, F3, NOTE, StaffettaAttachmentsTestCase, run_cli


class TestBrief(StaffettaAttachmentsTestCase):
    def setUp(self):
        super().setUp()
        self.pack(["--file", F1, "--file", F2, "--file", F3,
                   "--attach", F1, "--attach", F3])
        self.handoff = self.sender / "handoff"

    def brief(self, extra=()):
        return run_cli(["brief", "handoff", *extra], cwd=self.sender)

    def test_brief_with_ask_and_excerpts(self):
        result = self.brief(["--ask", "Какие два случая стоит добавить в тест?",
                             "--excerpt", f"{F1}:1-2", "--excerpt", f"{F1}:2"])
        self.assertEqual(result.returncode, 0, result.stderr)
        text = result.stdout
        # Сам вопрос и нужные строки кода на месте.
        self.assertIn("Какие два случая стоит добавить в тест?", text)
        self.assertIn("### src/parse_csv.py:1-2", text)
        self.assertIn("def parse_sales(path):", text)
        self.assertIn("```python", text)
        self.assertIn("### src/parse_csv.py:2-2", text)
        self.assertIn("raise NotImplementedError", text)
        # Контекст: цель, решение, что проверил отправитель, правила для чата.
        for needle in ("Цель:", "Принятое решение:", "Проверено отправителем",
                       "нет доступа к диску", "ответь на вопрос по фрагменту",
                       "локальные файлы не запрашивай"):
            self.assertIn(needle, text)
        # Никаких абсолютных путей Mac.
        self.assertNotIn(str(self.sender), text)
        self.assertNotIn("/Volumes/", text)
        self.assertNotIn("/Users/", text)

    def test_brief_without_ask_uses_next_step_from_manifest(self):
        result = self.brief()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("запустить тест разбора на этой машине", result.stdout)
        self.assertNotIn("```", result.stdout)  # без --excerpt кода нет
        # Без фрагментов вступление ведёт к записке, а не к отсутствующему коду.
        self.assertIn("ответь на вопрос по записке", result.stdout)
        self.assertNotIn("по фрагменту", result.stdout)
        self.assertNotIn("приведённые ниже фрагменты", result.stdout)

    def test_brief_shows_rejected_and_unknown_when_filled(self):
        result = self.brief()
        self.assertEqual(result.returncode, 0, result.stderr)
        text = result.stdout
        # Формулировки отправителя дословно, отдельными строками.
        self.assertIn("- Отвергнутый подход и причина: "
                      "править CSV вручную — файл перезаписывается", text)
        self.assertIn("- Что ещё не проверено: поведение на Windows не проверялось", text)
        self.assertLess(text.index("Отвергнутый подход и причина"),
                        text.index("Проверено отправителем"))
        self.assertLess(text.index("Что ещё не проверено"),
                        text.index("## Вопрос"))

    def test_brief_omits_rejected_and_unknown_lines_when_empty(self):
        result = run_cli(["pack", "--goal", "цель", "--decision", "решение",
                          "--next", "шаг", "--checked", "проверено на Mac",
                          "--out", "handoff3"],
                         cwd=self.sender)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = run_cli(["brief", "handoff3"], cwd=self.sender)
        self.assertEqual(result.returncode, 0, result.stderr)
        text = result.stdout
        self.assertNotIn("Отвергнутый подход", text)
        self.assertNotIn("Что ещё не проверено", text)
        self.assertNotIn("None", text)
        self.assertIn("проверено на Mac", text)

    def test_brief_old_manifest_without_rejected_unknown_reads_as_before(self):
        # Старая передача: ключей rejected/unknown в note нет вовсе.
        manifest_path = self.handoff / "manifest.json"
        data = __import__("json").loads(manifest_path.read_text(encoding="utf-8"))
        del data["note"]["rejected"]
        del data["note"]["unknown"]
        manifest_path.write_text(__import__("json").dumps(data, ensure_ascii=False),
                                 encoding="utf-8")
        result = self.brief()
        self.assertEqual(result.returncode, 0, result.stderr)
        text = result.stdout
        self.assertNotIn("Отвергнутый подход", text)
        self.assertNotIn("Что ещё не проверено", text)
        for needle in ("Цель:", "Принятое решение:", "Проверено отправителем",
                       "запустить тест разбора на этой машине"):
            self.assertIn(needle, text)

    def test_brief_excerpt_from_manifest_only_file_fails(self):
        result = self.brief(["--excerpt", f"{F2}:1-1"])  # F2 в манифесте, но не вложение
        self.assertEqual(result.returncode, 2)
        self.assertIn("не вложены", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_brief_bad_excerpt_specs_fail_with_empty_stdout(self):
        cases = (
            (F1, "ожидается ПУТЬ:НАЧАЛО"),
            (f"{F1}:abc", "некорректный диапазон"),
            (f"{F1}:0", "некорректный диапазон"),
            (f"{F1}:2-1", "некорректный диапазон"),
            (f"{F1}:1-99", "выходит за пределы файла"),
            ("nope.py:1-2", "нет в манифесте"),
        )
        for spec, needle in cases:
            result = self.brief(["--ask", "вопрос", "--excerpt", spec])
            self.assertEqual(result.returncode, 2, spec)
            self.assertIn(needle, result.stderr, spec)
            self.assertEqual(result.stdout, "", f"непустой stdout при ошибке: {spec}")

    def test_brief_corrupted_attachment_fails_with_empty_stdout(self):
        (self.handoff / "attachments" / "src" / "parse_csv.py").write_bytes(b"tampered")
        result = self.brief(["--ask", "вопрос", "--excerpt", f"{F1}:1-1"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("повреждено", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_brief_non_utf8_attachment_fails_with_empty_stdout(self):
        # Вложение совпадает по SHA-256 (pack записал его хэш), но не декодируется:
        # только так можно детерминированно дойти до проверки UTF-8.
        (self.sender / "data" / "blob.bin").write_bytes(b"\xff\xfe\x00binary")
        result = run_cli(["pack", *NOTE, "--file", "data/blob.bin",
                          "--attach", "data/blob.bin", "--out", "handoff-bin"],
                         cwd=self.sender)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = run_cli(["brief", "handoff-bin", "--ask", "вопрос",
                          "--excerpt", "data/blob.bin:1-1"], cwd=self.sender)
        self.assertEqual(result.returncode, 2)
        self.assertIn("не является текстом UTF-8", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_brief_excerpt_with_nested_fence_stays_intact(self):
        # Вложение содержит тройные бэктики: внешний забор обязан быть длиннее,
        # иначе фрагмент закроет его и перестанет быть понятным из текста.
        (self.sender / "data" / "notes.md").write_text(
            "# Заметки\n\n```bash\nexport PATH=\"$V/bin:$PATH\"\n```\n",
            encoding="utf-8")
        self.pack(["--file", "data/notes.md", "--attach", "data/notes.md",
                   "--force", "--out", "handoff"])
        result = run_cli(["brief", "handoff", "--ask", "вопрос",
                          "--excerpt", "data/notes.md:1-5"], cwd=self.sender)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("````markdown", result.stdout)
        self.assertIn('```bash\nexport PATH="$V/bin:$PATH"\n```', result.stdout)
        self.assertEqual(result.stdout.count("````"), 2, "заборы не парные")

    def test_brief_no_ask_and_no_next_step_fails(self):
        result = run_cli(
            ["pack", "--goal", "цель", "--decision", "решение", "--next", "шаг",
             "--file", F1, "--attach", F1, "--out", "handoff2"],
            cwd=self.sender)
        self.assertEqual(result.returncode, 0, result.stderr)
        handoff2 = self.sender / "handoff2"
        manifest_path = handoff2 / "manifest.json"
        data = __import__("json").loads(manifest_path.read_text(encoding="utf-8"))
        data["note"]["next"] = None
        manifest_path.write_text(__import__("json").dumps(data, ensure_ascii=False),
                                 encoding="utf-8")
        result = run_cli(["brief", "handoff2"], cwd=self.sender)
        self.assertEqual(result.returncode, 2)
        self.assertIn("вопрос сформулировать не из чего", result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
