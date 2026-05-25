from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import winreg
from dataclasses import dataclass
from pathlib import Path

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from typing import Callable


API_URL = "http://www.pandahome023.cn/analysis/api.php?id=188"
API_FORBIDDEN_TEXT = "禁止访问"
LICENSE_FILE_PATTERN = "license*.lic"


class LicenseError(RuntimeError):
    pass


@dataclass(frozen=True)
class LicenseInfo:
    machine_code: str
    license_path: Path
    owner: str


def get_machine_code() -> str:
    raw = "|".join(
        [
            _read_windows_machine_guid(),
            _read_wmic_uuid(),
        ]
    )
    digest = hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest().upper()
    return "-".join([digest[index : index + 8] for index in range(0, 32, 8)])


def get_license_search_dirs() -> list[Path]:
    paths: list[Path] = []
    app_dir = _get_app_dir()
    paths.append(app_dir)

    appdata = os.environ.get("APPDATA")
    if appdata:
        paths.append(Path(appdata) / "AutoOverlay")
    paths.append(Path.home() / ".autooverlay")
    return paths


def verify_local_license() -> LicenseInfo:
    machine_code = get_machine_code()
    license_path = _find_license_file()
    if license_path is None:
        search_text = "\n".join(str(path) for path in get_license_search_dirs())
        raise LicenseError(
            "未找到授权文件。\n\n"
            f"本机机器码：{machine_code}\n\n"
            f"请将授权文件命名为 license_用户名称_机器码.lic，放到以下任一目录：\n{search_text}"
        )

    try:
        payload = json.loads(license_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LicenseError("授权文件读取失败或格式不正确。") from exc

    license_machine_code = str(payload.get("machine_code", "")).strip().upper()
    expires_at = str(payload.get("expires_at", "")).strip()
    signature_text = str(payload.get("signature", "")).strip()
    owner = str(payload.get("owner", "")).strip() or "未命名用户"

    if license_machine_code != machine_code:
        raise LicenseError(
            "授权文件不属于当前设备。\n\n"
            f"当前机器码：{machine_code}\n"
            f"授权机器码：{license_machine_code or '空'}"
        )
    if expires_at and expires_at != "never" and expires_at < time.strftime("%Y-%m-%d"):
        raise LicenseError(f"授权文件已过期：{expires_at}")

    signed_payload = {
        "machine_code": license_machine_code,
        "owner": owner,
        "expires_at": expires_at or "never",
    }
    message = _canonical_json(signed_payload)
    try:
        signature = base64.b64decode(signature_text.encode("ascii"), validate=True)
    except Exception as exc:
        raise LicenseError("授权文件签名格式不正确。") from exc

    public_key_path = _get_public_key_path()
    if not public_key_path.exists():
        raise LicenseError(f"缺少公钥文件：{public_key_path}")
    public_key = serialization.load_pem_public_key(public_key_path.read_bytes())
    try:
        public_key.verify(signature, message)
    except InvalidSignature as exc:
        raise LicenseError("授权文件签名无效。") from exc

    return LicenseInfo(machine_code=machine_code, license_path=license_path, owner=owner)


def verify_remote_api(timeout: float = 5.0) -> None:
    _verify_remote_api_with_retry(timeout=timeout)


def verify_remote_api_with_progress(
    progress: Callable[[str], None] | None = None,
    request_timeout: float = 5.0,
    max_wait: float = 60.0,
    retry_interval: float = 5.0,
) -> None:
    _verify_remote_api_with_retry(
        timeout=request_timeout,
        max_wait=max_wait,
        retry_interval=retry_interval,
        progress=progress,
    )


def verify_export_permission(progress: Callable[[str], None] | None = None) -> LicenseInfo:
    info = verify_local_license()
    verify_remote_api_with_progress(progress=progress)
    return info


def _verify_remote_api_with_retry(
    timeout: float = 5.0,
    max_wait: float = 60.0,
    retry_interval: float = 5.0,
    progress: Callable[[str], None] | None = None,
) -> None:
    deadline = time.monotonic() + max_wait
    attempt = 1
    last_error = ""

    while True:
        remaining = max(0, int(deadline - time.monotonic()))
        if progress:
            progress(f"正在连接授权接口...剩余 {remaining} 秒")

        try:
            response = requests.get(url=API_URL, timeout=timeout)
        except requests.RequestException as exc:
            last_error = str(exc)
            if time.monotonic() >= deadline:
                raise LicenseError(f"接口连接超时，超过 {int(max_wait)} 秒仍无法连接：{last_error}") from exc
            _sleep_until_next_retry(deadline, retry_interval)
            attempt += 1
            continue

        if response.status_code != 200:
            raise LicenseError(f"接口校验失败，HTTP 状态码：{response.status_code}")
        if API_FORBIDDEN_TEXT in response.text:
            raise LicenseError("接口返回禁止访问，无法继续导出。")
        return


def _sleep_until_next_retry(deadline: float, retry_interval: float) -> None:
    sleep_seconds = min(retry_interval, max(0.0, deadline - time.monotonic()))
    if sleep_seconds > 0:
        time.sleep(sleep_seconds)


def _find_license_file() -> Path | None:
    candidates: list[Path] = []
    for directory in get_license_search_dirs():
        if not directory.exists() or not directory.is_dir():
            continue
        candidates.extend(path for path in directory.glob(LICENSE_FILE_PATTERN) if path.is_file())
    if not candidates:
        return None
    candidates.sort(key=lambda path: (path.name.lower() != "license.lic", path.name.lower()))
    return candidates[0]
    return None


def _get_app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _get_public_key_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "_internal" / "keys" / "license_public.pem"
    return Path(__file__).resolve().parent / "keys" / "license_public.pem"


def _read_windows_machine_guid() -> str:
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
            value, _value_type = winreg.QueryValueEx(key, "MachineGuid")
            return str(value)
    except OSError:
        return ""


def _read_wmic_uuid() -> str:
    try:
        result = subprocess.run(
            ["wmic", "csproduct", "get", "uuid"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=2,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""

    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) >= 2:
        return lines[1]
    return ""


def _canonical_json(payload: dict[str, str]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
