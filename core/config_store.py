"""設定檔加密工具。

從 main_window.py 提取的獨立模組，負責：
- API Key 的加密/解密儲存
- Windows 上優先使用 DPAPI（Data Protection API）
- 非 Windows 平台 fallback 到 Base64 混淆
"""
import base64
import os
import sys


def _is_windows():
    return sys.platform == 'win32'


def _dpapi_available():
    """檢查 Windows DPAPI（win32crypt）是否可用。"""
    if not _is_windows():
        return False
    try:
        import win32crypt  # noqa: F401
        return True
    except ImportError:
        return False


def obfuscate(key):
    """加密 API Key 用於設定檔儲存。

    Windows 上優先使用 DPAPI 加密（與當前使用者帳號綁定）；
    非 Windows 或無 win32crypt 時 fallback 到 Base64。
    """
    if not key:
        return ""
    if _dpapi_available():
        try:
            import win32crypt
            # DPAPI 加密：與當前使用者帳號綁定，其他使用者無法解密
            blob = win32crypt.CryptProtectData(
                key.encode('utf-8'),
                'MinecraftTranslatorAPIKey',  # description
                None, None, None, 0
            )
            # 用 Base64 包裝二進位 blob，方便 JSON 儲存
            return "DPAPI:" + base64.b64encode(blob).decode('ascii')
        except Exception:
            pass
    # Fallback: Base64 混淆（不是真正的加密，但避免明文）
    return base64.b64encode(key.encode('utf-8')).decode('ascii')


def deobfuscate(encoded):
    """解密設定檔中儲存的 API Key。

    自動偵測 DPAPI: 前綴 → 用 DPAPI 解密；
    無前綴 → 當作 Base64 處理（向後相容舊設定檔）。
    """
    if not encoded:
        return ""
    # DPAPI 加密的值
    if encoded.startswith("DPAPI:"):
        if _dpapi_available():
            try:
                import win32crypt
                blob = base64.b64decode(encoded[6:])
                _, plaintext = win32crypt.CryptUnprotectData(
                    blob, None, None, None, 0
                )
                return plaintext.decode('utf-8')
            except Exception:
                # DPAPI 解密失敗（可能換了使用者帳號），返回空字串
                # 使用者需要重新輸入 API Key
                return ""
        else:
            # 非 Windows 環境讀到 DPAPI 加密的值，無法解密
            return ""
    # 舊格式：Base64 混淆
    try:
        return base64.b64decode(encoded.encode('ascii')).decode('utf-8')
    except Exception:
        return encoded
