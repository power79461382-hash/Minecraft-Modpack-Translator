"""設定檔加密工具。

從 main_window.py 提取的獨立模組，負責：
- API Key 的加密/解密儲存
- Windows 上優先使用 DPAPI（Data Protection API）
- 非 Windows 平台 fallback 到 Base64 混淆
"""
import base64
import ctypes
from ctypes import wintypes
import json
import os
import sys
import tempfile


def _is_windows():
    return sys.platform == 'win32'


def _native_dpapi(data, decrypt=False):
    """Use Windows' built-in DPAPI even when pywin32 is not installed."""
    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data)
    source = DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = DataBlob()
    operation = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    operation.argtypes = [ctypes.POINTER(DataBlob),
                          ctypes.POINTER(wintypes.LPWSTR) if decrypt else wintypes.LPCWSTR,
                          ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p,
                          wintypes.DWORD, ctypes.POINTER(DataBlob)]
    operation.restype = wintypes.BOOL
    try:
        # CRYPTPROTECT_UI_FORBIDDEN: saving/loading must never open a native dialog.
        if not operation(ctypes.byref(source), None if decrypt else "MinecraftTranslatorAPIKey",
                         None, None, None, 1, ctypes.byref(result)):
            raise ctypes.WinError(ctypes.get_last_error())
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        if result.pbData:
            kernel32.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))


def _dpapi(data, decrypt=False):
    try:
        import win32crypt
    except ImportError:
        return _native_dpapi(data, decrypt)
    try:
        if decrypt:
            return win32crypt.CryptUnprotectData(data, None, None, None, 1)[1]
        return win32crypt.CryptProtectData(
            data, "MinecraftTranslatorAPIKey", None, None, None, 1)
    except Exception:
        return _native_dpapi(data, decrypt)


def obfuscate(key):
    """加密 API Key 用於設定檔儲存。

    Windows 上優先使用 DPAPI 加密（與當前使用者帳號綁定）；
    非 Windows 平台沿用 Base64；Windows 保護失敗時不降級儲存。
    """
    if not key:
        return ""
    if _is_windows():
        try:
            blob = _dpapi(key.encode('utf-8'))
            return "DPAPI:" + base64.b64encode(blob).decode('ascii')
        except Exception:
            raise OSError("Windows DPAPI could not protect the API key; settings were not saved.") from None
    # Fallback: Base64 混淆（不是真正的加密，但避免明文）
    return base64.b64encode(key.encode('utf-8')).decode('ascii')


def deobfuscate(encoded):
    """解密設定檔中儲存的 API Key。

    自動偵測 DPAPI: 前綴 → 用 DPAPI 解密；
    無前綴 → 當作 Base64 處理（向後相容舊設定檔）。
    """
    if not isinstance(encoded, str) or not encoded:
        return ""
    # DPAPI 加密的值
    if encoded.startswith("DPAPI:"):
        if _is_windows():
            try:
                blob = base64.b64decode(encoded[6:], validate=True)
                plaintext = _dpapi(blob, decrypt=True)
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
        return base64.b64decode(encoded.encode('ascii'), validate=True).decode('utf-8')
    except Exception:
        return ""


def write_json_atomic(path, data):
    """Replace a settings document only after its complete new contents are durable."""
    destination = os.path.abspath(os.fspath(path))
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=os.path.dirname(destination),
                prefix=".translator-config-", suffix=".tmp", delete=False) as stream:
            temporary = stream.name
            json.dump(data, stream, ensure_ascii=False, indent=4)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
