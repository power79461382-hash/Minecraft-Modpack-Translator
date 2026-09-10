import json

import pytest

from core import analysis_scan


def _make_instance(tmp_path, *, required_java=17, actual_java=21):
    instance_dir = tmp_path / "versions" / "Test Pack"
    instance_dir.mkdir(parents=True)
    version_data = {
        "clientVersion": "1.19.2",
        "javaVersion": {"majorVersion": required_java},
    }
    (instance_dir / "Test Pack.json").write_text(
        json.dumps(version_data), encoding="utf-8")
    logs_dir = instance_dir / "logs"
    logs_dir.mkdir()
    (logs_dir / "latest.log").write_text(
        f'Launcher: java version "{actual_java}.0.7"\n', encoding="utf-8")
    return instance_dir


def test_inspect_reports_java_21_incompatible_with_minecraft_1_19_2(tmp_path):
    instance_dir = _make_instance(tmp_path)

    report = analysis_scan.inspect_java_runtime_compatibility(
        str(instance_dir), minecraft_version="1.19.2")

    assert report["status"] == "incompatible"
    assert report["minecraft_version"] == "1.19.2"
    assert report["required_java_major"] == 17
    assert report["actual_java_major"] == 21
    assert report["is_incompatible"] is True
    assert report["version_json_path"] == str(
        instance_dir / "Test Pack.json")
    assert report["latest_log_path"] == str(
        instance_dir / "logs" / "latest.log")


@pytest.mark.parametrize(
    ("minecraft_version", "expected_status"),
    [("1.17", "incompatible"), ("1.20.4", "incompatible"),
     ("1.20.5", "not_applicable"), ("1.16.5", "not_applicable")],
)
def test_java_17_compatibility_range_has_precise_minecraft_boundaries(
        tmp_path, minecraft_version, expected_status):
    instance_dir = _make_instance(tmp_path)

    report = analysis_scan.inspect_java_runtime_compatibility(
        str(instance_dir), minecraft_version=minecraft_version)

    assert report["status"] == expected_status


def test_latest_log_reader_checks_bounded_head_and_tail(tmp_path):
    instance_dir = _make_instance(tmp_path)
    latest_log = instance_dir / "logs" / "latest.log"
    latest_log.write_bytes(
        b'java version "21.0.7"\n' + b'x' * (600 * 1024))

    report = analysis_scan.inspect_java_runtime_compatibility(
        str(instance_dir), minecraft_version="1.19.2")

    assert report["actual_java_major"] == 21
    assert report["latest_log_truncated"] is True
    assert report["latest_log_bytes_read"] <= 512 * 1024
    assert report["status"] == "incompatible"


def test_latest_log_reader_tolerates_invalid_utf8_and_missing_files(tmp_path):
    instance_dir = _make_instance(tmp_path)
    latest_log = instance_dir / "logs" / "latest.log"
    latest_log.write_bytes(
        b'\xff\xfeinvalid\njava version "21.0.7"\n')

    report = analysis_scan.inspect_java_runtime_compatibility(
        str(instance_dir), minecraft_version="1.19.2")
    assert report["actual_java_major"] == 21

    latest_log.unlink()
    (instance_dir / "Test Pack.json").unlink()
    missing_report = analysis_scan.inspect_java_runtime_compatibility(
        str(instance_dir), minecraft_version="1.19.2")
    assert missing_report["status"] == "not_applicable"
    assert missing_report["required_java_major"] is None
    assert missing_report["actual_java_major"] is None


def test_record_diagnostic_stores_report_and_logs_actionable_warning(tmp_path):
    instance_dir = _make_instance(tmp_path)

    class App:
        _detected_mc_version = "1.19.2"

        def __init__(self):
            self.messages = []

        def log(self, message):
            self.messages.append(message)

    app = App()

    report = analysis_scan.record_java_runtime_diagnostics(
        app, str(instance_dir), minecraft_version="1.19.2")

    assert app._java_runtime_compatibility_report is report
    combined = "\n".join(app.messages)
    assert "Java 21" in combined
    assert "Java 17" in combined
    assert "不是翻譯包或 Paxi 錯誤" in combined
    assert "會自動把 PCL" in combined


def test_java_17_installation_path_is_reported_when_available(
        tmp_path, monkeypatch):
    instance_dir = _make_instance(tmp_path, actual_java=25)
    java_exe = tmp_path / "jdk-17" / "bin" / "java.exe"
    java_exe.parent.mkdir(parents=True)
    java_exe.write_bytes(b"")
    monkeypatch.setattr(
        analysis_scan, "_java_runtime_candidates",
        lambda *_args: [str(java_exe)])

    report = analysis_scan.inspect_java_runtime_compatibility(
        str(instance_dir), minecraft_version="1.19.2")

    assert report["recommended_java_path"] == str(java_exe)
    assert str(java_exe) in "\n".join(
        analysis_scan.java_runtime_warning_lines(report))


def test_compatible_java_does_not_emit_warning(tmp_path):
    instance_dir = _make_instance(tmp_path, actual_java=17)
    report = analysis_scan.inspect_java_runtime_compatibility(
        str(instance_dir), minecraft_version="1.19.2")

    assert report["status"] == "compatible"
    assert analysis_scan.java_runtime_warning_lines(report) == []
