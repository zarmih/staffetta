"""Точка входа `python3 -m staffetta`: команды pack, check и restore-missing."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path, PurePosixPath

from . import core

STATUS_RU = {
    "match": "совпадает",
    "changed": "изменён",
    "missing": "отсутствует",
}

RESTORE_RU = {
    "restored": "восстановлен",
    "present": "уже на месте — не тронут",
    "changed": "изменён — не тронут",
    "appeared": "создан во время восстановления — его байты сохранены",
}


def _fail(message: str) -> int:
    print(f"Ошибка: {message}", file=sys.stderr)
    return 2


def cmd_pack(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    if not root.is_dir():
        return _fail(f"корневой каталог проекта не найден: {root}")

    def locate(raw: str):
        path = Path(raw)
        if not path.is_absolute():
            path = Path.cwd() / path
        return path.resolve()

    files = []
    seen = set()
    attached_rels = set()
    for raw in (args.file or []):
        path = locate(raw)
        if not core.is_under(path, root):
            return _fail(
                f"файл {raw} лежит вне корня проекта ({root}). "
                "Передайте путь относительно текущего каталога или укажите --root."
            )
        rel = core.rel_path_posix(path, root)
        if rel in seen:
            print(f"Предупреждение: {rel} указан повторно, включаю один раз.")
            continue
        seen.add(rel)
        files.append(path)

    for raw in (args.attach or []):
        path = locate(raw)
        if not core.is_under(path, root):
            return _fail(
                f"файл вложения {raw} лежит вне корня проекта ({root}). "
                "Передайте путь относительно текущего каталога или укажите --root."
            )
        rel = core.rel_path_posix(path, root)
        attached_rels.add(rel)
        if rel not in seen:
            seen.add(rel)
            files.append(path)

    note = {
        "goal": args.goal.strip(),
        "decision": args.decision.strip(),
        "next": args.next.strip(),
        "rejected": (args.rejected or "").strip() or None,
        "checked": (args.checked or "").strip() or None,
        "unknown": (args.unknown or "").strip() or None,
    }
    for flag, key in (("--goal", "goal"), ("--decision", "decision"), ("--next", "next")):
        if not note[key]:
            return _fail(f"{flag} не может быть пустым: в записке должны быть видны "
                         "цель, принятое решение и следующий шаг.")

    try:
        manifest = core.build_manifest(root, files, note, attached=attached_rels)
    except core.StaffettaError as exc:
        return _fail(str(exc))

    out = Path(args.out)
    if out.exists() and not args.force:
        return _fail(f"папка передачи {out} уже существует. "
                     "Укажите другую --out или повторите с --force.")
    try:
        # Собираем передачу во временном каталоге рядом и ставим одним
        # перемещением: посторонние пути не трогаем, прежняя передача при
        # сбое установки возвращается на место (см. core.install_handoff).
        out.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".staffetta-pack-", dir=out.parent))
        try:
            (staging / core.MANIFEST_NAME).write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            (staging / core.HANDOFF_NAME).write_text(
                core.render_handoff(manifest), encoding="utf-8"
            )
            source_by_rel = {core.rel_path_posix(p, root): p for p in files}
            for rel in sorted(attached_rels):
                dest = staging.joinpath(core.ATTACHMENTS_DIR, *PurePosixPath(rel).parts)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_by_rel[rel], dest)
            core.install_handoff(staging, out, force=args.force)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    except core.StaffettaError as exc:
        return _fail(str(exc))
    except OSError as exc:
        return _fail(f"не удалось записать папку передачи: {exc}")

    print(f"Папка передачи создана: {out.resolve()}")
    print(f"  {core.HANDOFF_NAME:<13} записка для человека или чата")
    print(f"  {core.MANIFEST_NAME:<13} {len(manifest['files'])} файл(ов) с SHA-256")
    if not manifest["files"]:
        print("  Предупреждение: файлы не выбраны — передаётся только записка.")
    if attached_rels:
        print(f"  {core.ATTACHMENTS_DIR + '/':<13} вложений: {len(attached_rels)} "
              "(байты внутри папки передачи)")
        print("Вложения уже внутри папки передачи; файлы без --attach передайте "
              "отдельно (git, общий диск, вручную).")
    else:
        print("Сами файлы pack не копирует: передайте их отдельно "
              "(git, общий диск, вручную).")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    handoff_dir = Path(args.handoff_dir)
    workdir = Path(args.workdir)
    try:
        manifest = core.load_manifest(handoff_dir / core.MANIFEST_NAME)
        if not workdir.is_dir():
            raise core.StaffettaError(
                f"рабочая копия не найдена или это не каталог: {workdir}"
            )
        results = core.run_check(manifest, workdir)
    except core.StaffettaError as exc:
        return _fail(str(exc))

    width = max((len(r["path"]) for r in results), default=0)
    for r in results:
        print(f"{r['path']:<{width}}  {STATUS_RU[r['status']]}")
    match = sum(1 for r in results if r["status"] == "match")
    changed = sum(1 for r in results if r["status"] == "changed")
    missing = sum(1 for r in results if r["status"] == "missing")
    print()
    print(f"Итог: {match} совпадает, {changed} изменён, {missing} отсутствует "
          f"(из {len(results)})")
    print("Рабочая копия проверена только на чтение; изменения не вносились.")
    return 0 if changed == 0 and missing == 0 else 1


def cmd_restore_missing(args: argparse.Namespace) -> int:
    handoff_dir = Path(args.handoff_dir)
    workdir = Path(args.workdir)
    try:
        manifest = core.load_manifest(handoff_dir / core.MANIFEST_NAME)
        if not workdir.is_dir():
            raise core.StaffettaError(
                f"рабочая копия не найдена или это не каталог: {workdir}"
            )
        results, notices = core.run_restore(manifest, handoff_dir, workdir,
                                            allow_nonatomic=args.allow_nonatomic)
    except core.AttachmentError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    except core.StaffettaError as exc:
        return _fail(str(exc))
    except OSError as exc:
        return _fail(f"ошибка файловой системы: {exc}")

    attached = [e for e in manifest["files"] if e.get("attached") is True]
    if not attached:
        print("В этой передаче нет вложений (pack выполнялся без --attach) — "
              "восстанавливать нечего.")
        return 0
    width = max((len(r["path"]) for r in results), default=0)
    for r in results:
        line = f"{r['path']:<{width}}  {RESTORE_RU[r['action']]}"
        if r.get("detail"):
            line += f" {r['detail']}"
        print(line)
    restored = sum(1 for r in results if r["action"] == "restored")
    present = sum(1 for r in results if r["action"] == "present")
    changed = sum(1 for r in results if r["action"] == "changed")
    appeared = sum(1 for r in results if r["action"] == "appeared")
    print()
    print(f"Итог: {restored} восстановлено, {present} уже на месте, {changed} изменённых "
          f"не тронуто, {appeared} появилось во время восстановления "
          f"(вложений: {len(attached)} из {len(manifest['files'])} файлов манифеста)")
    for notice in notices:
        print(notice)
    return 0


def _parse_excerpt(spec: str):
    """Разбирает spec «ПУТЬ:НАЧАЛО[-КОНЕЦ]» (нумерация строк с 1) и возвращает
    (путь, начало, конец). Некорректная запись — StaffettaError."""
    path, sep, range_part = spec.rpartition(":")
    if not sep or not path:
        raise core.StaffettaError(
            f"некорректный --excerpt {spec!r}: ожидается ПУТЬ:НАЧАЛО[-КОНЕЦ] "
            "(нумерация строк с 1)")
    if "-" in range_part:
        first, _, second = range_part.partition("-")
        if not (first.isdigit() and second.isdigit()):
            raise core.StaffettaError(f"некорректный диапазон в --excerpt {spec!r}")
        line_from, line_to = int(first), int(second)
    else:
        if not range_part.isdigit():
            raise core.StaffettaError(f"некорректный диапазон в --excerpt {spec!r}")
        line_from = line_to = int(range_part)
    if line_from < 1 or line_to < line_from:
        raise core.StaffettaError(
            f"некорректный диапазон в --excerpt {spec!r}: нужно 1 ≤ НАЧАЛО ≤ КОНЕЦ")
    return path, line_from, line_to


def cmd_brief(args: argparse.Namespace) -> int:
    handoff_dir = Path(args.handoff_dir)
    try:
        manifest = core.load_manifest(handoff_dir / core.MANIFEST_NAME)
        by_path = {e["path"]: e for e in manifest["files"]}
        attach_root = handoff_dir / core.ATTACHMENTS_DIR
        excerpts = []
        # Вся валидация — до единственного print: при любой проблеме stdout
        # остаётся пустым, чтобы пользователь не скопировал неполную записку.
        for spec in (args.excerpt or []):
            rel, line_from, line_to = _parse_excerpt(spec)
            entry = by_path.get(rel)
            if entry is None:
                raise core.StaffettaError(
                    f"--excerpt: файла нет в манифесте передачи: {rel}")
            if entry.get("attached") is not True:
                raise core.StaffettaError(
                    f"--excerpt: {rel} указан в манифесте, но его байты "
                    "не вложены в передачу (pack был без --attach)")
            source = core.safe_join(attach_root, rel, label="каталога вложений")
            if not source.is_file():
                raise core.StaffettaError(
                    f"--excerpt: вложение отсутствует в папке передачи: "
                    f"attachments/{rel}")
            if core.sha256_file(source) != entry["sha256"]:
                raise core.StaffettaError(
                    f"--excerpt: вложение повреждено — SHA-256 не совпадает "
                    f"с манифестом: attachments/{rel}")
            try:
                text = source.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                raise core.StaffettaError(
                    f"--excerpt: вложение не является текстом UTF-8: "
                    f"attachments/{rel}") from None
            file_lines = text.splitlines()
            if line_to > len(file_lines):
                raise core.StaffettaError(
                    f"--excerpt: диапазон выходит за пределы файла "
                    f"(всего строк: {len(file_lines)}): {spec}")
            excerpts.append((rel, line_from, line_to,
                             "\n".join(file_lines[line_from - 1:line_to])))
        ask = (args.ask or "").strip()
        if not ask:
            ask = (manifest.get("note", {}).get("next") or "").strip()
        if not ask:
            raise core.StaffettaError(
                "не задан --ask, а в записке нет следующего шага — "
                "вопрос сформулировать не из чего")
        print(core.render_brief(manifest, ask, excerpts))
        return 0
    except core.StaffettaError as exc:
        return _fail(str(exc))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="staffetta",
        description="Передача задачи между ИИ-агентами: pack собирает записку и "
                    "манифест (с вложениями по --attach), check сверяет файлы с "
                    "рабочей копией получателя, restore-missing возвращает "
                    "отсутствующие вложенные файлы.",
    )
    sub = parser.add_subparsers(dest="command", required=True,
                                metavar="{pack,check,restore-missing,brief}")

    p_pack = sub.add_parser("pack", help="собрать папку передачи: HANDOFF.md + manifest.json")
    p_pack.add_argument("--goal", required=True, help="цель и точка остановки")
    p_pack.add_argument("--decision", required=True, help="принятое решение и почему")
    p_pack.add_argument("--next", required=True, help="следующий шаг принимающей стороны")
    p_pack.add_argument("--rejected", help="отвергнутый подход и причина (необязательно)")
    p_pack.add_argument("--checked", help="что уже проверено, на какой машине и как (необязательно)")
    p_pack.add_argument("--unknown", help="что осталось неизвестным (необязательно)")
    p_pack.add_argument("--file", action="append", metavar="PATH",
                        help="файл для передачи (только SHA-256 в манифесте); "
                             "можно указывать несколько раз")
    p_pack.add_argument("--attach", action="append", metavar="PATH",
                        help="файл-вложение: запись в манифесте + байты в attachments/ "
                             "внутри папки передачи; можно указывать несколько раз. "
                             "Путь, указанный и в --file, и в --attach, даёт одну запись")
    p_pack.add_argument("--root", default=".",
                        help="корень проекта отправителя (по умолчанию текущий каталог)")
    p_pack.add_argument("--out", default="handoff",
                        help="папка передачи (по умолчанию ./handoff)")
    p_pack.add_argument("--force", action="store_true",
                        help="перезаписать существующую папку передачи")
    p_pack.set_defaults(func=cmd_pack)

    p_check = sub.add_parser("check",
                             help="сверить манифест с рабочей копией (только чтение)")
    p_check.add_argument("handoff_dir", help="папка передачи с manifest.json")
    p_check.add_argument("workdir", help="рабочая копия получателя")
    p_check.set_defaults(func=cmd_check)

    p_restore = sub.add_parser(
        "restore-missing",
        help="восстановить отсутствующие вложенные файлы из attachments/ "
             "(существующие и изменённые не трогает)",
    )
    p_restore.add_argument("handoff_dir",
                           help="папка передачи с manifest.json и attachments/")
    p_restore.add_argument("workdir", help="рабочая копия получателя")
    p_restore.add_argument(
        "--allow-nonatomic", action="store_true",
        help="разрешить неатомарную установку (O_EXCL) на томах без жёстких "
             "ссылок и исключительного переименования (например, exFAT). "
             "По умолчанию отключено: в этом режиме при прерывании записи "
             "файл может оказаться неполным; сверьте SHA-256 командой check")
    p_restore.set_defaults(func=cmd_restore_missing)

    p_brief = sub.add_parser(
        "brief",
        help="напечатать готовый текст для вставки в чат без доступа к диску",
    )
    p_brief.add_argument("handoff_dir", help="папка передачи с manifest.json")
    p_brief.add_argument("--ask", metavar="TEXT",
                         help="вопрос чату; по умолчанию — записанный "
                              "следующий шаг из манифеста")
    p_brief.add_argument("--excerpt", action="append", metavar="PATH:FROM[-TO]",
                         help="строки FROM..TO текстового вложения для вставки "
                              "(например src/parse_csv.py:1-20; нумерация с 1); "
                              "можно указывать несколько раз")
    p_brief.set_defaults(func=cmd_brief)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
