from pathlib import Path

from core.analysis_scan import apply_pcl_java_runtime_select
from core.translation_flow import confirm_java_runtime_before_translation


class _App:
    def __init__(self, report, analyzed_mc_dir=None):
        self._java_runtime_compatibility_report = report
        self.analyzed_mc_dir = analyzed_mc_dir
        self.prompts = []
        self.logs = []

    def _ask_proceed_from_thread(self, title, message):
        self.prompts.append((title, message))
        return False

    def log(self, message):
        self.logs.append(message)


def _incompatible_report(tmp_path, java_exe):
    return {
        "status": "incompatible",
        "is_incompatible": True,
        "minecraft_version": "1.20.1",
        "instance_dir": str(tmp_path),
        "required_java_major": 17,
        "actual_java_major": 21,
        "recommended_java_path": str(java_exe),
    }


def test_incompatible_runtime_auto_writes_pcl_setup_without_prompt(tmp_path):
    java_exe = tmp_path / "jdk-17" / "bin" / "java.exe"
    java_exe.parent.mkdir(parents=True)
    java_exe.write_text("", encoding="utf-8")
    instance = tmp_path / "versions" / "Demo"
    (instance / "PCL").mkdir(parents=True)
    setup = instance / "PCL" / "Setup.ini"
    setup.write_text("State:6\nInfo:demo\n", encoding="utf-8")

    app = _App(_incompatible_report(instance, java_exe), analyzed_mc_dir=str(instance))
    assert confirm_java_runtime_before_translation(app) is True
    assert app.prompts == []
    assert any("已自動將 PCL" in line for line in app.logs)

    content = setup.read_text(encoding="utf-8")
    assert "VersionArgumentJavaSelect:{" in content
    assert "jdk-17" in content.replace("\\", "/")
    assert "VersionArgumentJavaV2:3" in content
    assert "State:6" in content
    assert "Info:demo" in content


def test_incompatible_without_java_still_continues_without_prompt(tmp_path):
    app = _App({
        "status": "incompatible",
        "is_incompatible": True,
        "minecraft_version": "1.20.1",
        "instance_dir": str(tmp_path),
        "required_java_major": 17,
        "actual_java_major": 21,
        "recommended_java_path": None,
    })
    assert confirm_java_runtime_before_translation(app) is True
    assert app.prompts == []
    assert any("無法自動切換" in line for line in app.logs)


def test_compatible_or_unknown_runtime_does_not_prompt():
    app = _App({"status": "compatible"})
    assert confirm_java_runtime_before_translation(app) is True
    assert app.prompts == []


def test_apply_pcl_java_runtime_select_upserts_ini(tmp_path):
    java_exe = tmp_path / "bin" / "java.exe"
    java_exe.parent.mkdir(parents=True)
    java_exe.write_text("", encoding="utf-8")
    instance = tmp_path / "inst"
    ok, detail = apply_pcl_java_runtime_select(str(instance), str(java_exe), major=17)
    assert ok is True
    text = Path(detail).read_text(encoding="utf-8")
    assert "VersionArgumentJavaSelect:{" in text
    assert '"Path":' in text
    assert "VersionArgumentJavaV2:3" in text
