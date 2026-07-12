from core.translation_flow import confirm_java_runtime_before_translation


class _App:
    def __init__(self, answer):
        self._java_runtime_compatibility_report = {
            "status": "incompatible",
            "is_incompatible": True,
            "minecraft_version": "1.19.2",
            "required_java_major": 17,
            "actual_java_major": 25,
            "recommended_java_path": (
                r"C:\Program Files\Eclipse Adoptium\jdk-17\bin\java.exe"),
        }
        self.answer = answer
        self.prompts = []
        self.logs = []

    def _ask_proceed_from_thread(self, title, message):
        self.prompts.append((title, message))
        return self.answer

    def log(self, message):
        self.logs.append(message)


def test_java_25_blocks_translation_until_user_confirms_java_17_switch():
    app = _App(answer=False)

    assert confirm_java_runtime_before_translation(app) is False
    assert len(app.prompts) == 1
    title, message = app.prompts[0]
    assert "Java" in title
    assert "Java 25" in message
    assert "Java 17" in message
    assert "jdk-17" in message
    assert any("已取消" in line for line in app.logs)


def test_java_25_can_continue_only_after_explicit_confirmation():
    app = _App(answer=True)

    assert confirm_java_runtime_before_translation(app) is True
    assert len(app.prompts) == 1
    assert any("已確認" in line for line in app.logs)


def test_compatible_or_unknown_runtime_does_not_prompt():
    app = _App(answer=False)
    app._java_runtime_compatibility_report = {"status": "compatible"}

    assert confirm_java_runtime_before_translation(app) is True
    assert app.prompts == []
