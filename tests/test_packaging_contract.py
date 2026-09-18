from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_windows_build_contract_checks_artifact_and_checksum():
    script = (ROOT / "scripts" / "build-exe.ps1").read_text(encoding="utf-8")
    assert "PyInstaller" in script
    assert "Test-Path $artifact" in script
    assert "Get-FileHash $artifact -Algorithm SHA256" in script
    assert "SHA256SUMS.txt" in script
    assert "JuActl.exe" in script
    assert "packaging\\juactl.ico" in script
    assert "-m venv" in script
    assert "pyinstaller>=6,<7" in script


def test_windows_installer_contract():
    script = (ROOT / "scripts" / "build-installer.ps1").read_text(encoding="utf-8")
    iss = (ROOT / "packaging" / "JuActl.iss").read_text(encoding="utf-8")
    assert "Inno Setup 6" in script
    assert "choco.exe" in script
    assert "JuActl-Setup.exe" in script
    assert "PrivilegesRequired=lowest" in iss
    assert "JuActlBoard.exe" in iss
    assert "JuActl.exe" in iss
    assert "UninstallDisplayIcon" in iss
    assert "SetupIconFile" in iss


def test_ci_contract_separates_linux_tests_and_windows_package():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "linux-tests:" in workflow
    assert "windows-package:" in workflow
    assert "python tests/run_tests.py" in workflow
    assert "JuActlBoard.exe" in workflow
    assert "JuActl-Setup.exe" in workflow
    assert "SHA256SUMS.txt" in workflow
    assert "discover_hook_directories" in workflow
    assert "polluted environment" in workflow
    assert "Install and uninstall smoke" in workflow
    assert "JuActl-Setup.exe /VERYSILENT" in workflow
    assert "installedDoctor" in workflow
    assert "/LOG=$installLog" in workflow


def test_windows_qa_uses_the_same_isolated_package_builder():
    script = (ROOT / "scripts" / "qa.ps1").read_text(encoding="utf-8")
    assert "build-exe.ps1" in script
    assert "dist\\JuActl.exe" in script
    assert "doctor --json" in script
