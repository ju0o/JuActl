from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_windows_build_contract_checks_artifact_and_checksum():
    script = (ROOT / "scripts" / "build-exe.ps1").read_text(encoding="utf-8")
    assert "PyInstaller" in script
    assert "Test-Path $artifact" in script
    assert "Get-FileHash $artifact -Algorithm SHA256" in script
    assert "SHA256SUMS.txt" in script


def test_ci_contract_separates_linux_tests_and_windows_package():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "linux-tests:" in workflow
    assert "windows-package:" in workflow
    assert "python tests/run_tests.py" in workflow
    assert "JuActlBoard.exe" in workflow
    assert "SHA256SUMS.txt" in workflow
