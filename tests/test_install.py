import subprocess
from pathlib import Path


def test_git_install_script_installs_and_preserves_previous_version(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/install.sh"
    config = tmp_path / "config with spaces"
    config.mkdir()
    (config / "configuration.yaml").write_text("default_config:\n")
    for _ in range(2):
        result = subprocess.run(["sh", str(script), str(config)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    target = config / "custom_components/notion_backup"
    assert (target / "manifest.json").exists()
    assert (target / "config_flow.py").exists()
    previous = list(config.glob(".notion_backup_previous.*/notion_backup/manifest.json"))
    assert len(previous) == 1
    assert not list((config / "custom_components").glob(".notion_backup_stage.*"))


def test_install_rejects_wrong_config_path(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/install.sh"
    result = subprocess.run(["sh", str(script), str(tmp_path)], capture_output=True)
    assert result.returncode != 0
    assert not (tmp_path / "custom_components").exists()
