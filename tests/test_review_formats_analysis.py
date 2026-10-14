from types import SimpleNamespace

from core.analysis_scan import run_analyze_task


def analysis_app(work_dir, implementation):
    app = SimpleNamespace(
        _last_analysis_succeeded=True,
        stop_requested=False,
        failed_items_dir=str(work_dir),
        log=lambda *_args: None,
        _set_btn_state=lambda *_args: None,
        _set_summary_card=lambda *_args: None,
        root=SimpleNamespace(after=lambda _delay, callback: callback()),
        btn_analyze=None, btn_translate=None, btn_stop=None, btn_pause=None,
        C_DANGER="danger")
    app._analyze_task_impl = lambda path: implementation(app, path)
    return app


def test_failed_analysis_clears_previous_success(tmp_path):
    def fail(_app, _path):
        raise OSError("Synthetic scan failure")

    app = analysis_app(tmp_path, fail)
    run_analyze_task(app, str(tmp_path))

    assert app._last_analysis_succeeded is False


def test_stopped_analysis_is_not_success(tmp_path):
    def stop(app, _path):
        app.stop_requested = True

    app = analysis_app(tmp_path, stop)
    run_analyze_task(app, str(tmp_path))

    assert app._last_analysis_succeeded is False


def test_valid_empty_analysis_sets_success(tmp_path):
    app = analysis_app(tmp_path, lambda _app, _path: None)
    app._last_analysis_succeeded = False
    run_analyze_task(app, str(tmp_path))

    assert app._last_analysis_succeeded is True
