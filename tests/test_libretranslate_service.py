# -*- coding: utf-8 -*-
from core.libretranslate_service import parse_host_port, health_check, status


def test_parse_host_port_default():
    host, port = parse_host_port("http://127.0.0.1:5000")
    assert host == "127.0.0.1"
    assert port == 5000


def test_parse_host_port_bare():
    host, port = parse_host_port("127.0.0.1:5000")
    assert host == "127.0.0.1"
    assert port == 5000


def test_status_unhealthy_when_down():
    st = status("http://127.0.0.1:59999")
    assert st["healthy"] is False


def test_gui_has_lt_service_buttons():
    from pathlib import Path
    text = Path("gui/main_window.py").read_text(encoding="utf-8")
    assert "start_libretranslate_service" in text
    assert "btn_lt_start" in text
    assert "v1.2.14" in text
