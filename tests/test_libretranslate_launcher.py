from core.libretranslate_service import find_libretranslate_launcher

def test_launcher_prefers_main_module_or_cli():
    launcher = find_libretranslate_launcher()
    if launcher is None:
        return
    kind, prefix = launcher
    assert kind in ("cli", "module")
    joined = " ".join(prefix)
    assert "libretranslate.main" in joined or joined.endswith("libretranslate") or joined.endswith("libretranslate.exe")
    if kind == "module":
        assert prefix[-2:] == ["-m", "libretranslate.main"]
