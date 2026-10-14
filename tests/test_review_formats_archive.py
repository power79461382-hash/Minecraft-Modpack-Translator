import re
import zipfile

from core.jar_patcher import rebuild_jar_with_inject


def test_rebuild_preserves_archive_comment_and_unchanged_member_metadata(tmp_path):
    source = tmp_path / "source.jar"
    output = tmp_path / "translated.jar"
    info = zipfile.ZipInfo("unchanged.bin", date_time=(2024, 2, 3, 4, 5, 6))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.comment = b"member comment"
    info.external_attr = 0o100644 << 16
    with zipfile.ZipFile(source, "w") as jar:
        jar.comment = b"archive metadata marker"
        jar.writestr(info, b"original binary")

    rebuild_jar_with_inject(str(source), str(output),
                            {"assets/example/lang/zh_tw.json": b"{}"},
                            re.compile(r"^META-INF/[^/]+\.(SF|RSA|DSA|EC)$", re.I))

    with zipfile.ZipFile(output) as jar:
        assert jar.comment == b"archive metadata marker"
        assert jar.read("unchanged.bin") == b"original binary"
        rebuilt_info = jar.getinfo("unchanged.bin")
        assert rebuilt_info.date_time == (2024, 2, 3, 4, 5, 6)
        assert rebuilt_info.comment == b"member comment"
        assert rebuilt_info.external_attr == 0o100644 << 16
        assert rebuilt_info.compress_type == zipfile.ZIP_DEFLATED
