"""Ядро Staffetta: хэширование, манифест, сверка. Только стандартная библиотека."""
from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import shlex
import shutil
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from . import __version__

MANIFEST_FORMAT = "staffetta-manifest/1"
HANDOFF_NAME = "HANDOFF.md"
MANIFEST_NAME = "manifest.json"
ATTACHMENTS_DIR = "attachments"
_CHUNK = 64 * 1024


class StaffettaError(Exception):
    """Ожидаемая ошибка пользователя (неверный путь, повреждённый манифест и т.п.)."""


class AttachmentError(StaffettaError):
    """Вложение отсутствует или повреждено; восстановление не выполнялось."""


def sha256_file(path: Path) -> str:
    """SHA-256 файла, читая его потоково."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def rel_path_posix(path: Path, root: Path) -> str:
    """Путь файла относительно корня, всегда с «/», чтобы вид не зависел от ОС."""
    return Path(os.path.relpath(path, root)).as_posix()


def is_under(path: Path, root: Path) -> bool:
    """True, если path совпадает с root или лежит внутри него."""
    try:
        return os.path.commonpath([str(path), str(root)]) == str(root)
    except ValueError:
        return False


def safe_join(base: Path, rel_posix: str, label: str = "рабочей копии") -> Path:
    """Путь из манифеста внутри каталога-корня; запрещает выход за его пределы."""
    pure = PurePosixPath(rel_posix)
    if not pure.parts or pure.is_absolute() or ".." in pure.parts:
        raise StaffettaError(f"недопустимый путь в манифесте: {rel_posix!r}")
    candidate = base.joinpath(*pure.parts)
    real_base = os.path.realpath(base)
    real_target = os.path.realpath(candidate)
    if real_target != real_base and not real_target.startswith(real_base + os.sep):
        raise StaffettaError(
            f"путь из манифеста выходит за пределы {label}: {rel_posix!r}"
        )
    return candidate


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_manifest(root: Path, files, note: dict, attached=None) -> dict:
    """Манифест: относительный путь, размер и SHA-256 для каждого выбранного файла.

    attached — набор относительных путей, чьи байты pack кладёт в attachments/;
    такие записи дополнительно получают "attached": true, остальные — только хэш.
    """
    attached = attached or set()
    entries = []
    for path in files:
        if not path.is_file():
            raise StaffettaError(f"не найден или это не обычный файл: {path}")
        entry = {
            "path": rel_path_posix(path, root),
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
        }
        if entry["path"] in attached:
            entry["attached"] = True
        entries.append(entry)
    return {
        "format": MANIFEST_FORMAT,
        "tool": f"staffetta {__version__}",
        "created_at": utc_now_iso(),
        "root": root.name,
        "note": note,
        "files": entries,
    }


def load_manifest(path: Path) -> dict:
    """Читает и валидирует manifest.json; повреждённый или чужой формат — ошибка."""
    if not path.is_file():
        raise StaffettaError(f"манифест не найден: {path}. Сначала выполните pack.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise StaffettaError(f"манифест повреждён (не корректный JSON): {path}") from None
    if not isinstance(data, dict) or data.get("format") != MANIFEST_FORMAT:
        raise StaffettaError(
            f"неизвестный формат манифеста в {path}: ожидался {MANIFEST_FORMAT}"
        )
    files = data.get("files")
    if not isinstance(files, list):
        raise StaffettaError(f"в манифесте {path} нет списка files")
    for entry in files:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) \
                or not isinstance(entry.get("sha256"), str):
            raise StaffettaError(f"повреждённая запись файла в манифесте {path}")
        if "attached" in entry and not isinstance(entry["attached"], bool):
            raise StaffettaError(f"повреждённая запись файла в манифесте {path}")
    return data


def run_check(manifest: dict, workdir: Path) -> list:
    """Для каждой записи манифеста: match / changed / missing. Только чтение."""
    results = []
    for entry in manifest["files"]:
        target = safe_join(workdir, entry["path"])
        if not target.exists():
            status = "missing"
        elif not target.is_file():
            status = "changed"  # на месте файла оказался каталог или ссылка на каталог
        elif sha256_file(target) == entry["sha256"]:
            status = "match"
        else:
            status = "changed"
        results.append({"path": entry["path"], "status": status})
    return results


def _md_cell(text: str) -> str:
    return text.replace("|", "\\|")


_BRIEF_LANG_BY_SUFFIX = {".py": "python", ".json": "json", ".csv": "csv",
                         ".md": "markdown"}


def _code_fence(text: str) -> str:
    """Забор длиннее самой длинной цепочки бэктиков внутри text, чтобы вложенный
    блок (например, ```bash во фрагменте README) не закрыл внешний."""
    longest = run = 0
    for ch in text:
        if ch == "`":
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return "`" * max(3, longest + 1)


def render_brief(manifest: dict, ask: str, excerpts) -> str:
    """Готовая записка для вставки в чат без доступа к диску отправителя.

    Берёт из манифеста цель, решение, отвергнутый подход и результаты проверок
    отправителя; rejected и unknown попадают в записку только если заполнены —
    старые манифесты без этих ключей (или с null) читаются как раньше.
    ask — вопрос (или записанный следующий шаг); excerpts — список кортежей
    (относительный путь, от, до, текст строк) — только явно запрошенные
    фрагменты вложений. Без фрагментов записка компактнее: вступление
    предлагает ответить по самой записке, без ссылки на фрагменты.
    Абсолютных путей нет: только относительные пути манифеста и имя корня.
    """
    note = manifest.get("note", {})
    lines = []
    add = lines.append
    add("# Узкий вопрос по коду — записка для внешнего чата")
    add("")
    if excerpts:
        source = ("доступен только текст этой записки и приведённые ниже фрагменты. "
                  "Запуска команд и проверок здесь не выполняется — ответь на вопрос "
                  "по фрагменту, локальные файлы не запрашивай.")
    else:
        source = ("доступен только текст этой записки. "
                  "Запуска команд и проверок здесь не выполняется — ответь на вопрос "
                  "по записке, локальные файлы не запрашивай.")
    add(f"Проект: {manifest.get('root', '?')}. У этого чата нет доступа к диску "
        f"и файлам отправителя: {source}")
    add("")
    add("## Что сделано и проверено отправителем")
    add("")
    add(f"- Цель: {note.get('goal') or 'не указана'}")
    add(f"- Принятое решение: {note.get('decision') or 'не указано'}")
    rejected = note.get("rejected")
    if rejected:
        add(f"- Отвергнутый подход и причина: {rejected}")
    checked = note.get("checked")
    add("- Проверено отправителем (на его машине; в этом чате не выполнялось): "
        + (checked if checked else "не записано"))
    unknown = note.get("unknown")
    if unknown:
        add(f"- Что ещё не проверено: {unknown}")
    add("")
    add("## Вопрос")
    add("")
    add(ask)
    add("")
    for rel, line_from, line_to, text in excerpts:
        lang = _BRIEF_LANG_BY_SUFFIX.get(PurePosixPath(rel).suffix, "")
        fence = _code_fence(text)
        add(f"### {rel}:{line_from}-{line_to}")
        add("")
        add(fence + lang)
        add(text)
        add(fence)
        add("")
    add("---")
    add("Ответ передай исполнителю целиком — он продолжит работу с этими файлами.")
    return "\n".join(lines)


def _prepare_copy(source: Path, target: Path, expected_sha: str) -> Path:
    """Пишет байты источника во временный файл рядом с target и проверяет SHA-256
    подготовленной копии до её появления по конечному пути. По пути target ничего
    не создаётся, частичных записей нет."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=target.parent, prefix=".staffetta-restore-", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out, source.open("rb") as src:
            shutil.copyfileobj(src, out, _CHUNK)
            os.chmod(tmp, stat.S_IMODE(source.stat().st_mode))
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if sha256_file(tmp) != expected_sha:
        tmp.unlink(missing_ok=True)
        raise AttachmentError(
            f"подготовленная копия не совпала с манифестом: {source} — "
            "восстановление прервано"
        )
    return tmp


# На разных платформах ENOTSUP и EOPNOTSUPP могут совпадать или различаться,
# а ядро может вернуть любое из них (на exFAT macOS встречается ENOTSUP=45).
_LINK_UNSUPPORTED = frozenset({errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP,
                               errno.ENOSYS})
_RENAME_EXCL_UNSUPPORTED = frozenset({errno.EPERM, errno.EINVAL, errno.ENOTSUP,
                                      errno.EOPNOTSUPP, errno.ENOSYS})
_AT_FDCWD = -100
_RENAME_EXCL = 0x4
_RENAMEATX = None


def _get_renameatx():
    """renameatx_np из libSystem (macOS 10.12+); None, если символа нет."""
    global _RENAMEATX
    if _RENAMEATX is None:
        libc = ctypes.CDLL(None, use_errno=True)
        fn = getattr(libc, "renameatx_np", None)
        if fn is None:
            _RENAMEATX = False
        else:
            fn.argtypes = [ctypes.c_int, ctypes.c_char_p,
                           ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            fn.restype = ctypes.c_int
            _RENAMEATX = fn
    return _RENAMEATX or None


def _rename_noreplace_darwin(tmp: Path, target: Path) -> bool:
    """renameatx_np с RENAME_EXCL: атомарное «только создать» (darwin).

    True — файл создан; False — цель уже существует и не тронута. OSError
    с errno из _RENAME_EXCL_UNSUPPORTED — файловая система не поддерживает
    исключающее переименование (например, exFAT через fskit).
    """
    if sys.platform != "darwin":
        raise OSError(errno.ENOSYS, "renameatx_np доступен только на darwin")
    fn = _get_renameatx()
    if fn is None:
        raise OSError(errno.ENOSYS, "renameatx_np недоступен в этой системе")
    rc = fn(_AT_FDCWD, os.fsencode(str(tmp)),
            _AT_FDCWD, os.fsencode(str(target)), _RENAME_EXCL)
    if rc == 0:
        return True
    code = ctypes.get_errno()
    if code == errno.EEXIST:
        return False
    raise OSError(code, f"renameatx_np(RENAME_EXCL): {os.strerror(code)}")


def _install_o_excl(tmp: Path, target: Path) -> bool:
    """Последний уровень: создать target через O_CREAT|O_EXCL и записать
    уже проверенные байты. Перезапись исключена всегда; но там, где ФС не
    даёт атомарного «создать, если отсутствует» (например, exFAT — ни
    жёстких ссылок, ни RENAME_EXCL), видимость файла атомарной не является:
    при гибели процесса посреди записи получатель может увидеть частичный
    файл. Обычная ошибка записи убирает недописанный файл."""
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "wb") as out, tmp.open("rb") as src:
            shutil.copyfileobj(src, out, _CHUNK)
            os.chmod(target, stat.S_IMODE(tmp.stat().st_mode))
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return True


def _nonatomic_refusal(handoff_dir: Path, workdir: Path) -> str:
    """Сообщение об отказе: том не умеет атомарную установку «только создать»."""
    retry = shlex.join([
        "staffetta", "restore-missing", "--allow-nonatomic", "--",
        str(handoff_dir), str(workdir),
    ])
    return (
        "эта файловая система не поддерживает ни жёсткие ссылки, ни "
        "исключительное переименование — установить конечный файл только "
        "целиком здесь невозможно, и он не создан. Точная команда повтора:\n"
        f"  {retry}\n"
        "В этом режиме при прерывании записи файл может оказаться неполным — "
        "после восстановления сверьте его SHA-256 командой check."
    )


def _probe_install_tier(directory: Path) -> int:
    """Выясняет лучший доступный уровень установки, не трогая конечные файлы:
    1 — жёсткие ссылки, 2 — исключительное переименование, 3 — только
    неатомарный O_EXCL. Пробует временные имена и убирает их за собой."""
    fd, name = tempfile.mkstemp(prefix=".staffetta-probe-", dir=directory)
    src = Path(name)
    created = []
    try:
        with os.fdopen(fd, "wb"):
            pass
        try:
            dst = src.with_name(src.name + "-link")
            os.link(src, dst)
            created.append(dst)
            return 1
        except OSError:
            pass
        try:
            dst = src.with_name(src.name + "-rename")
            if _rename_noreplace_darwin(src, dst):
                created.append(dst)
            return 2  # уникальное имя: EEXIST здесь невозможен
        except OSError:
            pass
        return 3
    finally:
        for path in created:
            path.unlink(missing_ok=True)
        src.unlink(missing_ok=True)


def _install_created_only(tmp: Path, target: Path, refusal_text=None) -> tuple:
    """Создаёт target, только если по этому пути ещё ничего нет.

    Возвращает (установлен, уровень). Уровни по убыванию гарантий:
    1 — жёсткая ссылка: атомарно на APFS, HFS+, NTFS и большинстве Linux-ФС;
    2 — renameatx_np(RENAME_EXCL) на darwin: атомарно, если драйвер ФС
        поддерживает исключительное переименование;
    3 — O_EXCL-запись: перезапись исключена, атомарной видимости нет
        (ограничение файловой системы, см. _install_o_excl) — допускается
        только с refusal_text=None, то есть с явного согласия пользователя.
    False означает: к моменту установки цель уже существовала — ни один
    байт не перезаписан, подготовленную копию удаляет вызывающий код.
    """
    try:
        os.link(tmp, target)
        return True, 1
    except FileExistsError:
        return False, 1
    except OSError as exc:
        if exc.errno not in _LINK_UNSUPPORTED:
            raise
    try:
        return _rename_noreplace_darwin(tmp, target), 2
    except OSError as exc:
        if exc.errno not in _RENAME_EXCL_UNSUPPORTED:
            raise
    if refusal_text is not None:
        raise AttachmentError(refusal_text)
    return _install_o_excl(tmp, target), 3


def run_restore(manifest: dict, handoff_dir: Path, workdir: Path,
                before_install=None, allow_nonatomic=False) -> tuple:
    """Восстанавливает в workdir отсутствующие явно вложенные файлы из attachments/.

    Существующие и изменённые файлы не трогает. Целостность всех нужных вложений
    проверяет до первой записи: при проблеме ни один файл не записывается.
    По умолчанию конечный файл появляется только целиком: если оба атомарных
    способа недоступны (например, exFAT), восстановление завершается ошибкой
    ДО создания конечного файла и называет точную команду повтора; неатомарный
    режим O_EXCL включается только явным allow_nonatomic=True.
    Установка может только создать отсутствующий файл (жёсткая ссылка, затем
    renameatx_np(RENAME_EXCL), затем O_EXCL — см. _install_created_only): если
    между проверкой и сохранением файл создал кто-то другой, его байты остаются
    целыми, результат помечается action "appeared".
    Возвращает (результаты, уведомления): результаты — [{"path", "action",
    ("detail")}] для записей с "attached": true (передачи без вложений дают
    пустой список); уведомления — строки о способе установки, если файловая
    система не поддержала жёсткие ссылки.
    before_install — необязательный крючок между проверкой и установкой
    (детерминированные тесты гонки); в обычной работе не передаётся.
    """
    attach_root = handoff_dir / ATTACHMENTS_DIR
    actions, details = {}, {}
    to_restore = []
    for entry in manifest["files"]:
        if entry.get("attached") is not True:
            continue
        rel = entry["path"]
        target = safe_join(workdir, rel)
        source = safe_join(attach_root, rel, label="каталога вложений")
        if not target.exists():
            state = "missing"
        elif not target.is_file():
            state = "changed"  # на месте файла оказался каталог или ссылка на каталог
        elif sha256_file(target) == entry["sha256"]:
            state = "present"
        else:
            state = "changed"
        if state == "missing":
            to_restore.append((entry, target, source))
        else:
            actions[rel] = state

    problems = []
    for entry, _target, source in to_restore:
        if not source.is_file():
            problems.append(
                f"attachments/{entry['path']}: вложение отсутствует в папке передачи"
            )
        elif sha256_file(source) != entry["sha256"]:
            problems.append(
                f"attachments/{entry['path']}: вложение повреждено — "
                "SHA-256 не совпадает с манифестом"
            )
    if problems:
        raise AttachmentError(
            "восстановление не выполнено, ни один файл не записан:\n- "
            + "\n- ".join(problems)
        )

    refusal_text = None
    if to_restore and not allow_nonatomic:
        if _probe_install_tier(workdir) == 3:
            # Оба атомарных способа недоступны: отказ до создания конечного
            # файла, с точной командой повтора в режиме --allow-nonatomic.
            raise AttachmentError(_nonatomic_refusal(handoff_dir, workdir))
        refusal_text = _nonatomic_refusal(handoff_dir, workdir)

    if before_install is not None:
        before_install()

    max_tier = 0
    for entry, target, source in to_restore:
        rel = entry["path"]
        tmp = _prepare_copy(source, target, entry["sha256"])
        try:
            installed, tier = _install_created_only(tmp, target,
                                                    refusal_text=refusal_text)
        finally:
            tmp.unlink(missing_ok=True)
        if not installed:
            if target.is_file():
                actions[rel] = "appeared"
                details[rel] = ("(совпадает с манифестом)"
                                if sha256_file(target) == entry["sha256"]
                                else "(отличается от манифеста)")
            else:
                actions[rel] = "changed"  # на месте файла появился каталог
            continue
        if sha256_file(target) != entry["sha256"]:
            raise AttachmentError(
                f"записанный файл не совпал с манифестом: {rel} — "
                "проверьте диск и повторите"
            )
        max_tier = max(max_tier, tier)
        actions[rel] = "restored"

    notices = []
    if max_tier == 2:
        notices.append(
            "Примечание: жёсткие ссылки здесь недоступны — установка выполнена "
            "через renameatx_np(RENAME_EXCL): атомарно, без перезаписи."
        )
    elif max_tier == 3:
        notices.append(
            "Примечание: файловая система не поддерживает жёсткие ссылки и "
            "исключительное переименование (например, exFAT); файл установлен "
            "в режиме --allow-nonatomic (O_EXCL): перезапись существующих "
            "байтов исключена, но при прерывании записи файл может оказаться "
            "неполным — сверьте SHA-256 командой check."
        )
    results = [{"path": e["path"], "action": actions[e["path"]],
                **({"detail": details[e["path"]]} if e["path"] in details else {})}
               for e in manifest["files"] if e.get("attached") is True]
    return results, notices


_APPLEDOUBLE_MAGIC = b"\x00\x05\x16\x07"


def _is_appledouble_name(name: str) -> bool:
    """Имя вида «._X» — кандидат в AppleDouble-метаданные macOS для объекта X."""
    return name.startswith("._") and len(name) > 2


def _is_real_appledouble(path: Path) -> bool:
    """True, только если файл действительно начинается с магического числа
    формата AppleDouble (0x00051607). Обычный пользовательский файл, лишь
    названный «._X», отсекается; при любой ошибке чтения — тоже."""
    try:
        with path.open("rb") as fh:
            return fh.read(4) == _APPLEDOUBLE_MAGIC
    except OSError:
        return False


def _require_untouched_attachments(manifest: dict, out: Path) -> None:
    """Перед заменой сверяет содержимое attachments/ с прежним манифестом.

    Посторонний файл или прежнее вложение, изменённое вручную (SHA-256 не
    совпадает с манифестом), блокируют обновление с указанием пути. Прежнее
    вложение, отсутствующее на диске, удалять нечего — обновление разрешено.
    Файл «._X» пропускается только если это настоящий AppleDouble и X —
    объект, которым управляет передача (вложение из манифеста или каталог
    вложения); при сомнении путь называется посторонним.
    """
    expected = {e["path"]: e["sha256"]
                for e in manifest["files"] if e.get("attached") is True}
    attachments = out / ATTACHMENTS_DIR
    problems = []
    for stored in sorted(attachments.rglob("*")):
        if stored.is_dir():
            continue
        rel_parts = stored.relative_to(attachments).parts
        rel = PurePosixPath(*rel_parts).as_posix()
        if _is_appledouble_name(stored.name):
            counterpart = PurePosixPath(*rel_parts[:-1], stored.name[2:]).as_posix()
            managed = counterpart in expected or any(
                p.startswith(counterpart + "/") for p in expected)
            if _is_real_appledouble(stored) and managed:
                continue
            problems.append(
                f"attachments/{rel}: посторонний файл (имя вида ._X, но это "
                "не метаданные передачи или нет соответствующего объекта)")
            continue
        if rel not in expected:
            problems.append(f"attachments/{rel}: посторонний файл")
        elif sha256_file(stored) != expected[rel]:
            problems.append(
                f"attachments/{rel}: не совпадает с прежним манифестом "
                "(изменён вручную?)"
            )
    if problems:
        raise StaffettaError(
            f"{out}: в attachments/ есть данные, которых pack не создавал или "
            "которые изменены вручную — обновление прервано, содержимое "
            "сохранено:\n- " + "\n- ".join(problems)
        )


def _require_updatable_handoff(out: Path) -> None:
    """Разрешает --force-обновление только распознанной передачи Staffetta:
    валидный manifest.json, HANDOFF.md, ничего лишнего на верхнем уровне и
    attachments/, соответствующий прежнему манифесту."""
    if not (out / MANIFEST_NAME).is_file():
        raise StaffettaError(
            f"{out} существует, но это не папка передачи Staffetta "
            f"(нет {MANIFEST_NAME}). Содержимое сохранено; укажите другую --out "
            "или удалите каталог вручную."
        )
    try:
        manifest = load_manifest(out / MANIFEST_NAME)
    except StaffettaError as exc:
        raise StaffettaError(
            f"{out} содержит нечитаемый манифест ({exc}). Обновление прервано, "
            "содержимое сохранено; разберитесь с папкой вручную."
        ) from None
    if not (out / HANDOFF_NAME).is_file():
        raise StaffettaError(
            f"{out} похож на передачу, но в нём нет {HANDOFF_NAME}. "
            "Обновление прервано, содержимое сохранено."
        )
    attachments = out / ATTACHMENTS_DIR
    if attachments.exists() and not attachments.is_dir():
        raise StaffettaError(
            f"{out}: {ATTACHMENTS_DIR} — не каталог. Обновление прервано, "
            "содержимое сохранено."
        )
    allowed = {MANIFEST_NAME, HANDOFF_NAME, ATTACHMENTS_DIR}
    unknown = []
    for p in sorted(out.iterdir()):
        if p.name in allowed:
            continue
        if _is_appledouble_name(p.name) and _is_real_appledouble(p) \
                and p.name[2:] in allowed and (out / p.name[2:]).exists():
            continue  # метаданные macOS для управляемого объекта передачи
        unknown.append(p.name)
    if unknown:
        raise StaffettaError(
            f"{out} — папка передачи, но содержит неизвестные записи: "
            f"{', '.join(unknown)}. Обновление прервано, содержимое сохранено; "
            "удалите их вручную или укажите другую --out."
        )
    if attachments.is_dir():
        _require_untouched_attachments(manifest, out)


def _swap_dir(staging: Path, out: Path) -> None:
    """Подменяет out содержимым staging без этапа «удалил, но не поставил».

    Прежняя передача целиком переезжает на резервное имя; если установка новой
    сорвётся — возвращается на место и остаётся пригодной для check.
    """
    backup = Path(tempfile.mkdtemp(prefix=".staffetta-old-", dir=out.parent))
    backup.rmdir()  # освобождаем уникальное имя: rename не должен ничего перезаписать
    os.rename(out, backup)
    try:
        os.replace(staging, out)
    except BaseException:
        try:
            os.rename(backup, out)
        except OSError as exc:
            raise StaffettaError(
                f"установка сорвалась и откат не удался: прежняя передача "
                f"сохранена в {backup}"
            ) from exc
        raise
    shutil.rmtree(backup, ignore_errors=True)


def install_handoff(staging: Path, out: Path, force: bool = False) -> None:
    """Ставит передачу из staging в out, не разрушая посторонние данные.

    Отсутствующий out — простое перемещение. Обычный файл или символическая
    ссылка — отказ. Каталог обновляется только при force и только если это
    распознанная передача Staffetta; иначе отказ с сохранением содержимого.
    """
    if out.is_symlink() or (out.exists() and not out.is_dir()):
        raise StaffettaError(
            f"--out указывает на файл или ссылку, а не на папку передачи: {out}. "
            "Содержимое сохранено; укажите другую --out."
        )
    if out.is_dir():
        if not force:
            raise StaffettaError(
                f"папка передачи {out} уже существует. Укажите другую --out "
                "или повторите с --force."
            )
        _require_updatable_handoff(out)
        _swap_dir(staging, out)
    else:
        os.replace(staging, out)


def render_handoff(manifest: dict) -> str:
    """HANDOFF.md — записка от уходящего агента для человека и принимающего агента."""
    note = manifest.get("note", {})
    lines = []
    add = lines.append
    add("# Передача задачи — Staffetta")
    add("")
    add(f"Создано: {manifest.get('created_at', '?')} (UTC) · "
        f"проект-отправитель: {manifest.get('root', '?')} · "
        f"файлов в манифесте: {len(manifest.get('files', []))}")
    add("")
    add("Записка от уходящего агента — для человека и для принимающего ИИ-агента.")
    add("")
    add("## Цель и точка остановки")
    add("")
    add(note.get("goal", ""))
    add("")
    add("## Принятое решение")
    add("")
    add(note.get("decision", ""))
    if note.get("rejected"):
        add("")
        add(f"Отвергнутый подход: {note['rejected']}")
    add("")
    if note.get("checked"):
        add("## Проверено отправителем")
        add("")
        add(note["checked"])
        add("")
        add("Эти результаты получены на машине отправителя — на машине получателя "
            "они не проверялись.")
        add("")
    if note.get("unknown"):
        add("## Неизвестно / не проверялось")
        add("")
        add(note["unknown"])
        add("")
    add("## Следующий шаг")
    add("")
    add(note.get("next", ""))
    add("")
    add("## Файлы передачи")
    add("")
    add("| Путь | Размер (байт) | SHA-256 (первые 12) | Вложение |")
    add("|---|---|---|---|")
    for entry in manifest.get("files", []):
        attached_cell = "да" if entry.get("attached") is True else "—"
        add(f"| {_md_cell(entry['path'])} | {entry.get('size', '?')} | "
            f"`{entry['sha256'][:12]}` | {attached_cell} |")
    add("")
    has_attachments = any(e.get("attached") is True for e in manifest.get("files", []))
    if has_attachments:
        add("Файлы с пометкой «да» лежат внутри этой папки передачи в `attachments/`. "
            "Отсутствующие в рабочей копии восстанавливает команда "
            "`staffetta restore-missing \"<папка передачи>\" "
            "\"<рабочая копия>\"`: только отсутствующие вложенные файлы, "
            "существующие и изменённые она не перезаписывает.")
        add("")
        add("Если том получателя не поддерживает атомарную установку (например, "
            "exFAT), restore-missing по умолчанию откажется и напечатает точную "
            "команду повтора с флагом `--allow-nonatomic`. Режим выбирает "
            "пользователь, а не агент: добавляй этот флаг только по явному "
            "решению человека — при прерывании записи в этом режиме файл может "
            "оказаться неполным, после восстановления сверь SHA-256 командой "
            "check.")
        add("")
    add("Файлы без вложения команда `pack` не копирует — их передают отдельно "
        "(git, общий диск, вручную). Полные хэши и поля записки: `manifest.json`.")
    add("")
    add("## Инструкция принимающему агенту")
    add("")
    add("1. Прочитай записку выше и перечисли файлы из таблицы.")
    add("2. Если у тебя только текст этой записки и нет доступа к файлам — ты можешь "
        "выполнить смысловую часть (понять цель, решение, следующий шаг), но не проверку "
        "файлов: не изображай выполнение check; прямо скажи, что проверка не выполнялась, "
        "и запроси доступ к папке передачи и к рабочей копии.")
    add("3. Проверь файлы установленной командой `staffetta` из любого каталога:")
    add("")
    add("   ```")
    add("   staffetta check \"<папка, где лежит этот HANDOFF.md>\" "
        "\"<путь к рабочей копии получателя>\"")
    add("   ```")
    add("")
    add("   Первый аргумент — папка передачи (в ней manifest.json и этот файл), второй — "
        "рабочая копия получателя, куда доставлены файлы проекта. Пути с пробелами "
        "заключай в кавычки. Если Staffetta недоступна — сообщи об этом, не имитируя "
        "проверку.")
    add("4. Для каждого файла check выдаёт один из трёх статусов: совпадает / изменён / "
        "отсутствует. Отсутствующий локальный файл — нормальный результат (например, "
        "он не попал в Git), а не сбой.")
    add("5. Если в колонке «Вложение» стоит «да» и файл в рабочей копии отсутствует, "
        "восстанови его командой restore-missing (см. выше); она не трогает "
        "существующие и изменённые файлы. Флаг --allow-nonatomic добавляй только "
        "по явному решению пользователя, не по собственной инициативе. Файлы "
        "с «—» нужно получить отдельно.")
    add("6. Не выдавай раздел «Проверено отправителем» за результаты этой машины: свои "
        "выводы основывай только на командах, выполненных здесь.")
    add("7. Выполни «Следующий шаг» и отчитайся наблюдением (что именно вывела команда), "
        "а не интерпретацией.")
    add("")
    return "\n".join(lines)
