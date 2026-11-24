"""Регрессионные тесты сохранности данных:
--force не разрушает постороннее (файл, чужой каталог, неизвестные записи);
сорванная установка оставляет прежнюю передачу читаемой для check;
снятое вложение убирается из пакета при пересборке;
файл получателя, появившийся между проверкой и установкой, не перезаписывается.
Сбой и гонка проверяются детерминированно: точечная подмена os.replace
и крючок before_install в core.run_restore, без случайных задержек."""
import contextlib
import errno
import io
import json
import os
import shlex
import unittest
from pathlib import Path
from unittest import mock

import staffetta.__main__ as staffetta_cli
import staffetta.core as core

from test_attachments import F1, F2, F3, NOTE, StaffettaAttachmentsTestCase, run_cli

FILES_ALL = ("--file", F1, "--file", F2, "--file", F3)


class TestForceRebuildSafety(StaffettaAttachmentsTestCase):
    def test_force_refuses_unknown_file_in_handoff(self):
        handoff = self.pack([*FILES_ALL, "--attach", F3])
        old_manifest = (handoff / "manifest.json").read_bytes()
        (handoff / "notes.txt").write_text("заметки участника", encoding="utf-8")

        result = run_cli(["pack", *NOTE, "--force", *FILES_ALL, "--attach", F3,
                          "--out", "handoff"], cwd=self.sender)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("неизвестные записи", result.stderr)
        self.assertIn("notes.txt", result.stderr)
        self.assertTrue((handoff / "notes.txt").is_file(), "посторонний файл потерян")
        self.assertEqual((handoff / "manifest.json").read_bytes(), old_manifest)

    def test_force_refuses_foreign_directory_and_regular_file(self):
        foreign = self.base / "not-a-handoff"
        foreign.mkdir()
        (foreign / "stuff.txt").write_text("постороннее", encoding="utf-8")
        result = run_cli(["pack", *NOTE, "--force", "--file", F1,
                          "--out", str(foreign)], cwd=self.sender)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("не папка передачи", result.stderr)
        self.assertEqual((foreign / "stuff.txt").read_text(encoding="utf-8"),
                         "постороннее")
        self.assertFalse((foreign / "manifest.json").exists(), "чужой каталог перезаписан")

        plain = self.base / "plain.txt"
        plain.write_bytes(b"keep me")
        result = run_cli(["pack", *NOTE, "--force", "--file", F1,
                          "--out", str(plain)], cwd=self.sender)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(plain.read_bytes(), b"keep me")

    def test_failed_install_keeps_previous_handoff_checkable(self):
        handoff = self.pack([*FILES_ALL, "--attach", F3])
        old_manifest = (handoff / "manifest.json").read_bytes()
        old_csv = (handoff / "attachments" / "data" / "sales_local.csv").read_bytes()
        (self.sender / F3).write_text("date;amount\n2026-12-25;77,77\n", encoding="utf-8")

        real_replace = os.replace

        def failing_replace(src, dst, *args, **kwargs):
            # Сбой установки строго после того, как прежняя передача уехала
            # на резервное имя: падает только перемещение staging → out.
            if Path(src).name.startswith(".staffetta-pack-"):
                raise OSError(28, "No space left on device")
            return real_replace(src, dst, *args, **kwargs)

        out_buf, err_buf = io.StringIO(), io.StringIO()
        files_abs = []
        for rel in (F1, F2, F3):
            files_abs += ["--file", str(self.sender / rel)]
        with mock.patch("staffetta.core.os.replace", side_effect=failing_replace), \
                contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            code = staffetta_cli.main(["pack", *NOTE, "--force", *files_abs,
                                       "--attach", str(self.sender / F3),
                                       "--root", str(self.sender),
                                       "--out", str(handoff)])
        self.assertEqual(code, 2)
        self.assertIn("не удалось записать", err_buf.getvalue())

        # Прежняя передача вернулась на место: байты и пригодность для check.
        self.assertEqual((handoff / "manifest.json").read_bytes(), old_manifest)
        self.assertEqual(
            (handoff / "attachments" / "data" / "sales_local.csv").read_bytes(), old_csv)
        recipient = self.base / "recipient-rollback"
        for rel, data in ((F1, (self.sender / F1).read_bytes()),
                          (F2, (self.sender / F2).read_bytes()),
                          (F3, old_csv)):
            (recipient / rel).parent.mkdir(parents=True, exist_ok=True)
            (recipient / rel).write_bytes(data)
        check = run_cli(["check", "handoff", str(recipient)], cwd=self.sender)
        self.assertEqual(check.returncode, 0, check.stdout)
        self.assertIn("3 совпадает", check.stdout)
        self.assertEqual(list(self.sender.glob(".staffetta-pack-*")), [])
        self.assertEqual(list(self.sender.glob(".staffetta-old-*")), [])

    def test_force_rebuild_drops_deselected_attachment(self):
        self.pack([*FILES_ALL, "--attach", F3])
        result = run_cli(["pack", *NOTE, "--force", *FILES_ALL, "--attach", F1,
                          "--out", "handoff"], cwd=self.sender)
        self.assertEqual(result.returncode, 0, result.stderr)
        handoff = self.sender / "handoff"
        self.assertTrue((handoff / "attachments" / "src" / "parse_csv.py").is_file())
        self.assertFalse((handoff / "attachments" / "data").exists(),
                         "снятое вложение осталось в пакете")
        manifest = json.loads((handoff / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual([e["path"] for e in manifest["files"] if e.get("attached")], [F1])

    def test_force_updates_v01_style_handoff_without_attachments(self):
        self.pack(FILES_ALL)  # передача без вложений, как в v0.1
        manifest = json.loads(
            (self.sender / "handoff" / "manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(all("attached" not in e for e in manifest["files"]))
        result = run_cli(["pack", *NOTE, "--force", *FILES_ALL, "--attach", F3,
                          "--out", "handoff"], cwd=self.sender)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            (self.sender / "handoff" / "attachments" / "data" / "sales_local.csv").is_file())


class TestRestoreRace(StaffettaAttachmentsTestCase):
    def test_recipient_file_created_between_check_and_install_is_kept(self):
        handoff = self.pack(["--file", F3, "--attach", F3])
        manifest = core.load_manifest(handoff / "manifest.json")
        workdir = self.base / "late-recipient"
        workdir.mkdir()
        foreign_bytes = "локальные байты, записанные вторым агентом".encode("utf-8")

        def second_agent_creates_file():
            # Детерминированная гонка: файл появляется строго между
            # проверкой отсутствия и установкой.
            (workdir / F3).parent.mkdir(parents=True, exist_ok=True)
            (workdir / F3).write_bytes(foreign_bytes)

        results, notices = core.run_restore(manifest, handoff, workdir,
                                            before_install=second_agent_creates_file)

        by_path = {r["path"]: r for r in results}
        self.assertEqual(by_path[F3]["action"], "appeared")
        self.assertEqual(by_path[F3]["detail"], "(отличается от манифеста)")
        self.assertEqual((workdir / F3).read_bytes(), foreign_bytes,
                         "байты, созданные вторым участником, перезаписаны")
        self.assertEqual(list((workdir / "data").glob(".staffetta-*")), [],
                         "осталась невостребованная подготовленная копия")
        self.assertEqual(notices, [], "ничего не установлено — уведомлений быть не должно")


def link_unsupported(*_args, **_kwargs):
    """Свежий OSError(ENOTSUP) на каждый вызов — как у exFAT при os.link."""
    raise OSError(errno.ENOTSUP, "Operation not supported")


class TestInstallStrategies(StaffettaAttachmentsTestCase):
    """Каскад установки: link → renameatx_np(RENAME_EXCL) → O_EXCL.
    Каждая стратегия проверяется детерминированной подменой, без реального exFAT."""

    def prepare_restore(self, workdir_name="recipient"):
        handoff = self.pack(["--file", F3, "--attach", F3])
        manifest = core.load_manifest(handoff / "manifest.json")
        workdir = self.base / workdir_name
        workdir.mkdir()
        return handoff, manifest, workdir

    def test_fallback_renameatx_restores_verified_bytes(self):
        handoff, manifest, workdir = self.prepare_restore()
        with mock.patch("staffetta.core.os.link", side_effect=link_unsupported):
            results, notices = core.run_restore(manifest, handoff, workdir)
        self.assertEqual(results[0]["action"], "restored")
        self.assertEqual((workdir / F3).read_bytes(), (self.sender / F3).read_bytes())
        self.assertIn("renameatx_np(RENAME_EXCL)", notices[0])

    def test_fallback_o_excl_restores_verified_bytes(self):
        handoff, manifest, workdir = self.prepare_restore("recipient-o-excl")
        with mock.patch("staffetta.core.os.link", side_effect=link_unsupported), \
                mock.patch("staffetta.core._rename_noreplace_darwin",
                           side_effect=link_unsupported):
            results, notices = core.run_restore(manifest, handoff, workdir,
                                                allow_nonatomic=True)
        self.assertEqual(results[0]["action"], "restored")
        self.assertEqual((workdir / F3).read_bytes(), (self.sender / F3).read_bytes())
        self.assertIn("O_EXCL", notices[0])
        self.assertIn("--allow-nonatomic", notices[0])

    def test_race_kept_with_renameatx_fallback(self):
        handoff, manifest, workdir = self.prepare_restore("late-renameatx")
        foreign_bytes = b"bytes from the second agent"

        def second_agent_creates_file():
            # Детерминированная гонка: файл появляется между проверкой
            # и установкой; установка идёт через renameatx_np(RENAME_EXCL).
            (workdir / F3).parent.mkdir(parents=True, exist_ok=True)
            (workdir / F3).write_bytes(foreign_bytes)

        with mock.patch("staffetta.core.os.link", side_effect=link_unsupported):
            results, notices = core.run_restore(manifest, handoff, workdir,
                                                before_install=second_agent_creates_file)
        self.assertEqual(results[0]["action"], "appeared")
        self.assertEqual((workdir / F3).read_bytes(), foreign_bytes)
        self.assertEqual(notices, [])

    def test_race_kept_with_o_excl_fallback(self):
        handoff, manifest, workdir = self.prepare_restore("late-o-excl")
        foreign_bytes = b"bytes from the second agent"

        def second_agent_creates_file():
            # Гонка при установке через O_EXCL: создание не может перезаписать.
            (workdir / F3).parent.mkdir(parents=True, exist_ok=True)
            (workdir / F3).write_bytes(foreign_bytes)

        with mock.patch("staffetta.core.os.link", side_effect=link_unsupported), \
                mock.patch("staffetta.core._rename_noreplace_darwin",
                           side_effect=link_unsupported):
            results, notices = core.run_restore(manifest, handoff, workdir,
                                                before_install=second_agent_creates_file,
                                                allow_nonatomic=True)
        self.assertEqual(results[0]["action"], "appeared")
        self.assertEqual((workdir / F3).read_bytes(), foreign_bytes)
        self.assertEqual(notices, [])


class TestNonatomicRefusal(StaffettaAttachmentsTestCase):
    """По умолчанию конечный файл появляется только целиком: на «томе» без
    атомарных способов (эмулируется подменой) — отказ до создания файла."""

    def test_default_refuses_before_creating_final_file(self):
        handoff = self.pack(["--file", F3, "--attach", F3])
        manifest = core.load_manifest(handoff / "manifest.json")
        workdir = self.base / "refusal recipient '$work'"
        workdir.mkdir()

        with mock.patch("staffetta.core.os.link", side_effect=link_unsupported), \
                mock.patch("staffetta.core._rename_noreplace_darwin",
                           side_effect=link_unsupported):
            with self.assertRaises(core.AttachmentError) as ctx:
                core.run_restore(manifest, handoff, workdir)

        message = str(ctx.exception)
        self.assertEqual(shlex.split(message.splitlines()[1]), [
            "staffetta", "restore-missing", "--allow-nonatomic", "--",
            str(handoff), str(workdir),
        ])
        self.assertIn("неполным", message)
        self.assertFalse((workdir / F3).exists(), "конечный файл создан вопреки отказу")
        self.assertEqual(list(workdir.glob(".staffetta-*")), [],
                         "проба оставила временные файлы")

    def test_probe_reports_link_tier_on_normal_fs(self):
        tier = core._probe_install_tier(self.base)
        self.assertEqual(tier, 1)
        self.assertEqual(list(self.base.glob(".staffetta-probe-*")), [])

    def test_probe_reports_o_excl_tier_when_both_unsupported(self):
        with mock.patch("staffetta.core.os.link", side_effect=link_unsupported), \
                mock.patch("staffetta.core._rename_noreplace_darwin",
                           side_effect=link_unsupported):
            self.assertEqual(core._probe_install_tier(self.base), 3)
        self.assertEqual(list(self.base.glob(".staffetta-probe-*")), [])


class TestAttachmentsIntegrity(StaffettaAttachmentsTestCase):
    def test_force_refuses_foreign_file_inside_attachments(self):
        handoff = self.pack([*FILES_ALL, "--attach", F3])
        old_manifest = (handoff / "manifest.json").read_bytes()
        note = handoff / "attachments" / "data" / "заметка-пользователя.txt"
        note.write_text("личные пометки к передаче", encoding="utf-8")

        result = run_cli(["pack", *NOTE, "--force", *FILES_ALL, "--attach", F3,
                          "--out", "handoff"], cwd=self.sender)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("attachments/data/заметка-пользователя.txt", result.stderr)
        self.assertIn("посторонний файл", result.stderr)
        self.assertTrue(note.is_file(), "пользовательская заметка потеряна")
        self.assertEqual((handoff / "manifest.json").read_bytes(), old_manifest)
        self.assertTrue(
            (handoff / "attachments" / "data" / "sales_local.csv").is_file())

    def test_force_refuses_manually_modified_old_attachment(self):
        handoff = self.pack([*FILES_ALL, "--attach", F3])
        old_manifest = (handoff / "manifest.json").read_bytes()
        stored = handoff / "attachments" / "data" / "sales_local.csv"
        stored.write_text("date;amount\n1;1\n", encoding="utf-8")  # правка руками

        result = run_cli(["pack", *NOTE, "--force", *FILES_ALL, "--attach", F3,
                          "--out", "handoff"], cwd=self.sender)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("attachments/data/sales_local.csv", result.stderr)
        self.assertIn("не совпадает с прежним манифестом", result.stderr)
        self.assertEqual(stored.read_text(encoding="utf-8"), "date;amount\n1;1\n")
        self.assertEqual((handoff / "manifest.json").read_bytes(), old_manifest)

    def test_force_allows_missing_old_attachment_and_rebuilds_it(self):
        self.pack([*FILES_ALL, "--attach", F3])
        (self.sender / "handoff" / "attachments" / "data" / "sales_local.csv").unlink()
        result = run_cli(["pack", *NOTE, "--force", *FILES_ALL, "--attach", F3,
                          "--out", "handoff"], cwd=self.sender)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            (self.sender / "handoff" / "attachments" / "data" / "sales_local.csv").is_file())

    def test_force_ignores_real_appledouble_sidecars_of_managed_objects(self):
        # macOS создаёт ._X рядом с файлами pack (например, на exFAT) — это
        # метаданные ОС: настоящий AppleDouble-формат и управляемая пара X.
        self.pack([*FILES_ALL, "--attach", F3])
        handoff = self.sender / "handoff"
        ad = b"\x00\x05\x16\x07" + b"\x00" * 8  # магия + версия AppleDouble
        (handoff / "._manifest.json").write_bytes(ad)
        (handoff / "._HANDOFF.md").write_bytes(ad)
        (handoff / "._attachments").write_bytes(ad)
        (handoff / "attachments" / "._data").write_bytes(ad)  # каталог вложения
        (handoff / "attachments" / "data" / "._sales_local.csv").write_bytes(ad)
        result = run_cli(["pack", *NOTE, "--force", *FILES_ALL, "--attach", F3,
                          "--out", "handoff"], cwd=self.sender)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_force_refuses_fake_appledouble_user_note(self):
        # ._notes — обычный пользовательский файл: нет магии AppleDouble и
        # нет управляемой пары; заметка должна пережить --force без изменений.
        handoff = self.pack([*FILES_ALL, "--attach", F3])
        old_manifest = (handoff / "manifest.json").read_bytes()
        note = handoff / "attachments" / "._notes"
        note.write_text("мои личные пометки", encoding="utf-8")

        result = run_cli(["pack", *NOTE, "--force", *FILES_ALL, "--attach", F3,
                          "--out", "handoff"], cwd=self.sender)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("attachments/._notes", result.stderr)
        self.assertIn("посторонний файл", result.stderr)
        self.assertEqual(note.read_text(encoding="utf-8"), "мои личные пометки")
        self.assertEqual((handoff / "manifest.json").read_bytes(), old_manifest)

    def test_force_refuses_valid_magic_without_managed_counterpart(self):
        # Настоящая магия AppleDouble, но пары в передаче нет — сомнение
        # трактуется в пользу пользователя: отказ с названным путём.
        handoff = self.pack([*FILES_ALL, "--attach", F3])
        ad = b"\x00\x05\x16\x07" + b"\x00" * 8
        orphan = handoff / "._README.md"
        orphan.write_bytes(ad)

        result = run_cli(["pack", *NOTE, "--force", *FILES_ALL, "--attach", F3,
                          "--out", "handoff"], cwd=self.sender)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("._README.md", result.stderr)
        self.assertEqual(orphan.read_bytes(), ad)

    def test_force_refuses_sidecar_of_missing_counterpart_in_root(self):
        # ._attachments при отсутствии самого каталога attachments/ — сомнительно.
        handoff = self.pack(FILES_ALL)  # без вложений: attachments/ нет
        ad = b"\x00\x05\x16\x07" + b"\x00" * 8
        (handoff / "._attachments").write_bytes(ad)
        result = run_cli(["pack", *NOTE, "--force", *FILES_ALL, "--attach", F3,
                          "--out", "handoff"], cwd=self.sender)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("._attachments", result.stderr)


if __name__ == "__main__":
    unittest.main()
