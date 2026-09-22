import os
import tempfile
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import repo_context


def main():
    with tempfile.TemporaryDirectory(prefix="tank_repo_priority_") as tmp:
        root = Path(tmp).resolve()
        (root / "pyproject.toml").write_text("[project]\nname = \"kofi-archiver\"\n", encoding="utf-8")
        main_file = root / "kofi_archiver.py"
        main_file.write_text(
            "import helper\n"
            "def main():\n"
            "    print('start')\n"
            + ("# middle filler\n" * 6000)
            + "def final_result():\n"
            "    return 'tail visible'\n",
            encoding="utf-8",
        )
        (root / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
        (root / "README.md").write_text("Run kofi_archiver.py to archive Ko-Fi data.\n", encoding="utf-8")
        (root / "diagnostics.log").write_text("log\n" * 100, encoding="utf-8")
        (root / "cookies.txt").write_text("sessionid=secret\n", encoding="utf-8")
        (root / "setup_installer.msi").write_bytes(b"installer")
        outside = root.parent / f"{root.name}_outside.py"
        outside.write_text("print('outside')\n", encoding="utf-8")

        cfg = {"max_context_files": 4, "max_file_bytes": 32000}
        selected = repo_context.select_repo_files(str(root), "Fix Ko-Fi Archiver kofi archiver mission", cfg)
        assert selected[0] == "kofi_archiver.py", selected
        assert "diagnostics.log" not in selected[:2], selected

        context_cfg = dict(cfg)
        context_cfg["_discovered_count"] = repo_context.repo_file_count(str(root))
        collection = repo_context.collect_repo_files(
            str(root),
            selected + ["cookies.txt", "setup_installer.msi", str(outside), "diagnostics.log"],
            context_cfg,
        )
        files = collection["files"]
        assert "kofi_archiver.py" in files
        assert "Tank context note: oversized file included as truncated excerpt" in files["kofi_archiver.py"]
        assert "import helper" in files["kofi_archiver.py"]
        assert "tail visible" in files["kofi_archiver.py"]
        assert collection["included_truncated_count"] >= 1
        assert collection["discovered"] >= len(selected)
        assert collection["selected"] >= len(selected)

        assert "cookies.txt" not in files
        assert "setup_installer.msi" not in files
        reasons = {item["path"]: item["reason"] for item in collection["skipped"]}
        assert "sensitive file skipped" in reasons["cookies.txt"]
        assert "binary or unsupported" in reasons["setup_installer.msi"]
        assert any("outside workspace" in item["reason"] for item in collection["skipped"])
        assert any(item["path"] == "diagnostics.log" for item in collection["skipped"])

    print("repo context priority validation passed")


if __name__ == "__main__":
    main()
