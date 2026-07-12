import re
import warnings
import zipfile

import pytest

from core.jar_patcher import rebuild_jar_with_inject


JAR_SIGNATURE_RE = re.compile(
    r"^META-INF/.*\.(?:SF|RSA|DSA|EC)$", re.IGNORECASE)


def test_rebuild_preserves_duplicate_member_payloads_and_order(tmp_path):
    source_jar = tmp_path / "source.jar"
    rebuilt_jar = tmp_path / "rebuilt.jar"

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(source_jar, "w") as archive:
            archive.writestr("dup.bin", b"first")
            archive.writestr("dup.bin", b"second")

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        rebuild_jar_with_inject(
            str(source_jar),
            str(rebuilt_jar),
            {"assets/example/lang/zh_tw.json": b"{}"},
            JAR_SIGNATURE_RE,
        )

    with zipfile.ZipFile(rebuilt_jar) as archive:
        duplicate_infos = [
            item for item in archive.infolist() if item.filename == "dup.bin"
        ]
        duplicate_payloads = [archive.read(item) for item in duplicate_infos]

    assert duplicate_payloads == [b"first", b"second"]


@pytest.mark.parametrize("newline", [b"\r\n", b"\n"])
def test_rebuild_removes_only_manifest_digest_headers_and_continuations(
        tmp_path, newline):
    source_jar = tmp_path / "signed.jar"
    rebuilt_jar = tmp_path / "rebuilt.jar"
    resource_path = "assets/example/lang/zh_tw.json"
    manifest = newline.join([
        b"Manifest-Version: 1.0",
        b"Created-By: archive-integrity-test",
        b"SHA-256-Digest-Manifest: main-digest",
        b" main-digest-continuation",
        b"Long-Main-Value: keep-main",
        b" keep-main-continuation",
        b"X-Digest-Policy: preserve-this-metadata",
        b" preserve-this-continuation",
        b"",
        f"Name: {resource_path}".encode("ascii"),
        b"SHA-256-Digest: entry-digest",
        b" entry-digest-continuation",
        b"X-Keep-Entry: yes",
        b"",
        b"Name: com/example/",
        b"Sealed: true",
        b"X-Keep-Package: yes",
        b"",
        b"",
    ])

    with zipfile.ZipFile(source_jar, "w") as archive:
        archive.writestr("META-INF/MANIFEST.MF", manifest)
        archive.writestr("META-INF/TEST.SF", b"stale signature")
        archive.writestr(resource_path, b"old")

    stripped_signature = rebuild_jar_with_inject(
        str(source_jar),
        str(rebuilt_jar),
        {resource_path: b"new"},
        JAR_SIGNATURE_RE,
    )

    with zipfile.ZipFile(rebuilt_jar) as archive:
        rebuilt_manifest = archive.read("META-INF/MANIFEST.MF")
        names = archive.namelist()

    assert stripped_signature is True
    assert "META-INF/TEST.SF" not in names
    assert b"SHA-256-Digest-Manifest:" not in rebuilt_manifest
    assert b"SHA-256-Digest:" not in rebuilt_manifest
    assert b"digest-continuation" not in rebuilt_manifest
    assert b"Manifest-Version: 1.0" in rebuilt_manifest
    assert b"Created-By: archive-integrity-test" in rebuilt_manifest
    assert b"Long-Main-Value: keep-main" + newline + b" keep-main-continuation" in rebuilt_manifest
    assert (b"X-Digest-Policy: preserve-this-metadata" + newline
            + b" preserve-this-continuation") in rebuilt_manifest
    assert f"Name: {resource_path}".encode("ascii") in rebuilt_manifest
    assert b"X-Keep-Entry: yes" in rebuilt_manifest
    assert b"Name: com/example/" + newline + b"Sealed: true" in rebuilt_manifest
    assert b"X-Keep-Package: yes" in rebuilt_manifest
    assert rebuilt_manifest.count(newline + newline) == 3
    assert rebuilt_manifest.endswith(newline + newline)
