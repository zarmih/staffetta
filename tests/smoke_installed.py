"""Run with the installed package's Python from a directory with the copied demo."""
import json
import os
import shutil
import subprocess
import sys
from importlib.metadata import distribution
from pathlib import Path
from tempfile import TemporaryDirectory

PARSER = Path("src/parse_csv.py")
TEST = Path("tests/test_parse_csv.py")
CSV = Path("data/sales_local.csv")


def run(command, cwd, expected=0):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    env.pop("PYTHONPATH", None)
    result = subprocess.run(command, cwd=cwd, capture_output=True,
                            text=True, encoding="utf-8", env=env)
    assert result.returncode == expected, result
    return result


def smoke(base, demo, cli, version):
    sender, recipient, transfer = (base / name for name in (
        "копия отправителя", "копия получателя", "передача задачи"
    ))
    shutil.copytree(demo / "sender-work", sender)
    shutil.copytree(demo / "recipient-work", recipient)
    assert run([cli, "--version"], base).stdout.strip() == f"staffetta {version}"
    run([cli, "pack", "--goal", "проверить разбор CSV",
         "--decision", "сохранить локальные правки",
         "--next", "запустить тест разбора CSV",
         "--file", PARSER, "--file", TEST, "--attach", PARSER,
         "--attach", CSV, "--out", transfer], sender)
    manifest = json.loads((transfer / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["tool"] == f"staffetta {version}"
    assert [item["path"] for item in manifest["files"]] == [
        PARSER.as_posix(), TEST.as_posix(), CSV.as_posix()
    ]
    assert sorted(p.relative_to(transfer / "attachments") for p in
                  (transfer / "attachments").rglob("*") if p.is_file()) == [CSV, PARSER]
    for path in (PARSER, CSV):
        assert (transfer / "attachments" / path).read_bytes() == (sender / path).read_bytes()

    run([cli, "check", transfer, sender], sender)
    output = run([cli, "check", transfer, recipient], recipient, 1).stdout
    for path, status in ((PARSER, "совпадает"), (TEST, "изменён"), (CSV, "отсутствует")):
        assert f"{path} {status}" in " ".join(output.split()), output

    brief = run([cli, "brief", transfer, "--ask", "Что проверить?",
                 "--excerpt", f"{PARSER}:7-14"], recipient).stdout
    excerpt = (sender / PARSER).read_text(encoding="utf-8").splitlines()[6:14]
    assert "Что проверить?" in brief and "\n".join(excerpt) in brief
    for spec, error in ((f"{TEST}:1", "не вложены"), (f"{PARSER}:²", "диапазон")):
        result = run([cli, "brief", transfer, "--excerpt", spec], recipient, 2)
        assert result.stdout == "" and error in result.stderr, result

    original = {path: (recipient / path).read_bytes() for path in (PARSER, TEST)}
    output = run([cli, "restore-missing", transfer, recipient], recipient).stdout
    assert "1 восстановлено, 1 уже на месте" in output, output
    assert (recipient / CSV).read_bytes() == (sender / CSV).read_bytes()
    for path, data in original.items():
        assert (recipient / path).read_bytes() == data
    output = run([sys.executable, TEST], recipient).stdout
    assert "OK: разобрано строк: 2; первая: 2026-11-20 = 1234.50" in output, output
    output = run([cli, "check", transfer, recipient], recipient, 1).stdout
    assert "2 совпадает, 1 изменён, 0 отсутствует" in output, output
    assert "0 восстановлено" in run(
        [cli, "restore-missing", transfer, recipient], recipient
    ).stdout

    locally_edited = original[PARSER] + b"\n# local edit\n"
    (recipient / PARSER).write_bytes(locally_edited)
    output = run([cli, "restore-missing", transfer, recipient], recipient).stdout
    assert "изменён — не тронут" in output, output
    assert (recipient / PARSER).read_bytes() == locally_edited

    (recipient / CSV).unlink()
    (transfer / "attachments" / CSV).write_bytes(b"corrupt")
    result = run([cli, "restore-missing", transfer, recipient], recipient, 1)
    assert "SHA-256 не совпадает" in result.stderr, result
    assert not (recipient / CSV).exists()
    assert (recipient / PARSER).read_bytes() == locally_edited
    assert (recipient / TEST).read_bytes() == original[TEST]


if __name__ == "__main__":
    package = distribution("staffetta")
    assert Path(package.locate_file("staffetta")).resolve().is_relative_to(sys.prefix)
    cli = Path(sys.executable).with_name("staffetta")
    with TemporaryDirectory() as directory:
        smoke(Path(directory), Path("demo").resolve(), cli, package.version)
    print(f"Installed Staffetta {package.version} passed on Python {sys.version.split()[0]}")
