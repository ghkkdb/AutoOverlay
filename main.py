from __future__ import annotations

import json
import os
import queue
import re
import threading
import tkinter as tk
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from licensing import LicenseError, get_machine_code, verify_export_permission
from overlay_engine import (
    OverlayError,
    OverlaySettings,
    estimate_required_assets,
    probe_video,
    render_overlay_video,
    scan_video_assets,
    validate_settings,
)


UNUSABLE_ASSET_POLICY_OPTIONS = {
    "导出时询问": "ask",
    "跳过这些素材继续": "skip",
    "停止导出": "abort",
}
ASSET_EXHAUSTION_POLICY_OPTIONS = {
    "导出时询问": "ask",
    "重新打乱循环复用": "reuse",
    "用完后续就不插入了": "stop",
    "停止导出": "abort_export",
}
SHORT_ASSET_POLICY_OPTIONS = {
    "导出时询问": "ask",
    "继续，自动裁短": "trim",
    "停止导出": "abort",
}
PROBE_WORKER_OPTIONS = {
    "自动": "auto",
    "1": "1",
    "2": "2",
    "4": "4",
    "6": "6",
    "8": "8",
}
MAX_AUTO_PROBE_WORKERS = 8


class AutoOverlayApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("AutoOverlay")
        self.geometry("760x520")
        self.minsize(720, 480)

        self.message_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        self.preflight_thread: threading.Thread | None = None
        self.cancel_event = threading.Event()
        self.config_path = self._get_config_path()
        self.current_main_duration = 0.0
        self.placeholders: dict[str, str] = {}

        self.main_video_var = tk.StringVar()
        self.asset_folder_var = tk.StringVar()
        self.horizontal_asset_folder_var = tk.StringVar()
        self.output_path_var = tk.StringVar()
        self.clip_start_min_var = tk.StringVar(value="5")
        self.clip_start_max_var = tk.StringVar(value="5")
        self.clip_end_min_var = tk.StringVar(value="8")
        self.clip_end_max_var = tk.StringVar(value="8")
        self.interval_min_var = tk.StringVar(value="5")
        self.interval_max_var = tk.StringVar(value="10")
        self.unusable_asset_policy_var = tk.StringVar(value="ask")
        self.asset_exhaustion_policy_var = tk.StringVar(value="ask")
        self.short_asset_policy_var = tk.StringVar(value="ask")
        self.probe_worker_var = tk.StringVar(value="auto")

        self._load_config()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll_worker_messages)

    def _build_ui(self) -> None:
        """
        创建主窗口界面控件。

        Returns:
            None: 无返回值。
        """
        root = ttk.Frame(self, padding=16)
        root.pack(fill=tk.BOTH, expand=True)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(9, weight=1)

        self._add_path_row(root, 0, "主视频", self.main_video_var, self._choose_main_video, "请选择主视频文件", "file")
        self._add_path_row(root, 1, "素材文件夹", self.asset_folder_var, self._choose_asset_folder, "请选择小视频素材文件夹", "folder")
        self._add_path_row(
            root,
            2,
            "横屏素材文件夹",
            self.horizontal_asset_folder_var,
            self._choose_horizontal_asset_folder,
            "可留空；不为空时会把横屏素材随机叠加到视频底部",
            "folder",
        )
        self._add_path_row(root, 3, "输出文件", self.output_path_var, self._choose_output_path, "请选择导出视频保存位置", "output")

        time_frame = ttk.LabelFrame(root, text="小视频截取片段", padding=12)
        time_frame.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(12, 0))
        ttk.Label(time_frame, text="起始最小秒").grid(row=0, column=0, sticky="w")
        ttk.Entry(time_frame, textvariable=self.clip_start_min_var, width=9).grid(row=0, column=1, sticky="w", padx=(8, 18))
        ttk.Label(time_frame, text="起始最大秒").grid(row=0, column=2, sticky="w")
        ttk.Entry(time_frame, textvariable=self.clip_start_max_var, width=9).grid(row=0, column=3, sticky="w", padx=(8, 18))
        ttk.Label(time_frame, text="结束最小秒").grid(row=0, column=4, sticky="w")
        ttk.Entry(time_frame, textvariable=self.clip_end_min_var, width=9).grid(row=0, column=5, sticky="w", padx=(8, 18))
        ttk.Label(time_frame, text="结束最大秒").grid(row=0, column=6, sticky="w")
        ttk.Entry(time_frame, textvariable=self.clip_end_max_var, width=9).grid(row=0, column=7, sticky="w", padx=(8, 0))

        interval_frame = ttk.LabelFrame(root, text="随机主视频插入间隔", padding=12)
        interval_frame.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(12, 0))
        interval_frame.columnconfigure(1, weight=1)
        interval_frame.columnconfigure(3, weight=1)
        ttk.Label(interval_frame, text="最小秒").grid(row=0, column=0, sticky="w")
        ttk.Entry(interval_frame, textvariable=self.interval_min_var, width=12).grid(row=0, column=1, sticky="w", padx=(8, 24))
        ttk.Label(interval_frame, text="最大秒").grid(row=0, column=2, sticky="w")
        ttk.Entry(interval_frame, textvariable=self.interval_max_var, width=12).grid(row=0, column=3, sticky="w", padx=(8, 0))

        button_frame = ttk.Frame(root)
        button_frame.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(14, 0))
        button_frame.columnconfigure(0, weight=1)
        self.start_button = ttk.Button(button_frame, text="开始导出", command=self._start_export)
        self.start_button.grid(row=0, column=1, padx=(0, 8))
        self.cancel_button = ttk.Button(button_frame, text="取消", command=self._cancel_export, state=tk.DISABLED)
        self.cancel_button.grid(row=0, column=2)
        self.open_output_button = ttk.Button(button_frame, text="打开输出文件夹", command=self._open_output_folder)
        self.open_output_button.grid(row=0, column=3, padx=(8, 0))
        self.machine_code_button = ttk.Button(button_frame, text="复制机器码", command=self._copy_machine_code)
        self.machine_code_button.grid(row=0, column=4, padx=(8, 0))
        self.settings_button = ttk.Button(button_frame, text="设置", command=self._show_settings)
        self.settings_button.grid(row=0, column=5, padx=(8, 0))
        self.help_button = ttk.Button(button_frame, text="使用说明", command=self._show_help)
        self.help_button.grid(row=0, column=6, padx=(8, 0))

        self.progress_var = tk.StringVar(value="等待开始。")
        ttk.Label(root, textvariable=self.progress_var).grid(row=7, column=0, columnspan=4, sticky="ew", pady=(14, 6))
        self.progress_bar = ttk.Progressbar(root, mode="determinate", maximum=100)
        self.progress_bar.grid(row=8, column=0, columnspan=4, sticky="ew", pady=(0, 8))

        self.log_text = tk.Text(root, height=10, wrap=tk.WORD, state=tk.DISABLED)
        self.log_text.grid(row=9, column=0, columnspan=4, sticky="nsew")
        scrollbar = ttk.Scrollbar(root, orient=tk.VERTICAL, command=self.log_text.yview)
        scrollbar.grid(row=9, column=4, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def _add_path_row(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
        command: Callable[[], None],
        placeholder: str,
        path_kind: str,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=5)
        entry = tk.Entry(parent, textvariable=variable, fg="black")
        entry.grid(row=row, column=1, sticky="ew", padx=(12, 8), pady=5)
        self.placeholders[str(variable)] = placeholder
        self._apply_placeholder(entry, variable)
        entry.bind("<FocusIn>", lambda _event: self._clear_placeholder(entry, variable))
        entry.bind("<FocusOut>", lambda _event: self._apply_placeholder(entry, variable))
        ttk.Button(parent, text="选择", command=command).grid(row=row, column=2, sticky="e", pady=5)
        ttk.Button(
            parent,
            text="打开",
            command=lambda: self._open_selected_path(variable, path_kind),
        ).grid(row=row, column=3, sticky="e", padx=(8, 0), pady=5)

    def _choose_main_video(self) -> None:
        path = filedialog.askopenfilename(
            title="选择主视频",
            filetypes=[("视频文件", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v"), ("所有文件", "*.*")],
        )
        if not path:
            return
        self.main_video_var.set(path)
        source = Path(path)
        self.output_path_var.set(str(source.with_name(f"{source.stem}_auto_overlay.mp4")))
        self._save_config()

    def _choose_asset_folder(self) -> None:
        path = filedialog.askdirectory(title="选择小视频素材文件夹")
        if path:
            self.asset_folder_var.set(path)
            self._save_config()

    def _choose_horizontal_asset_folder(self) -> None:
        """
        选择横屏素材文件夹。

        Returns:
            None: 无返回值。
        """
        path = filedialog.askdirectory(title="选择横屏素材文件夹")
        if path:
            self.horizontal_asset_folder_var.set(path)
            self._save_config()

    def _choose_output_path(self) -> None:
        path = filedialog.asksaveasfilename(
            title="选择输出文件",
            defaultextension=".mp4",
            filetypes=[("MP4 视频", "*.mp4")],
        )
        if path:
            self.output_path_var.set(path)
            self._save_config()

    def _start_export(self) -> None:
        if self._is_busy():
            return

        try:
            settings = self._read_settings(asset_exhaustion_policy="abort", short_asset_policy="error")
            validate_settings(settings)
        except OverlayError as exc:
            self._show_error("参数错误", str(exc))
            return

        self.cancel_event.clear()
        self._set_running(True)
        self._clear_log()
        self.progress_bar.configure(value=0)
        self.progress_bar.configure(mode="indeterminate")
        self.progress_bar.start(10)
        self.progress_var.set("正在校验授权...")
        self._append_log("正在校验授权...")

        self.preflight_thread = threading.Thread(
            target=self._preflight_worker,
            args=(settings,),
            daemon=True,
        )
        self.preflight_thread.start()

    def _read_settings(self, asset_exhaustion_policy: str, short_asset_policy: str) -> OverlaySettings:
        """
        从界面读取导出设置。

        Args:
            asset_exhaustion_policy (str): 素材不足处理策略。
            short_asset_policy (str): 素材时长不足处理策略。

        Returns:
            OverlaySettings: 导出设置。

        Raises:
            OverlayError: 时间、路径等参数不合法时抛出。
        """
        try:
            clip_start_min = float(self.clip_start_min_var.get())
            clip_start_max = float(self.clip_start_max_var.get())
            clip_end_min = float(self.clip_end_min_var.get())
            clip_end_max = float(self.clip_end_max_var.get())
            interval_min = float(self.interval_min_var.get())
            interval_max = float(self.interval_max_var.get())
        except ValueError as exc:
            raise OverlayError("时间和间隔必须填写数字。") from exc

        output_text = self._var_value(self.output_path_var)
        if not output_text:
            raise OverlayError("请选择输出文件。")
        output_path = Path(output_text)

        return OverlaySettings(
            main_video=Path(self._var_value(self.main_video_var)),
            asset_folder=Path(self._var_value(self.asset_folder_var)),
            horizontal_asset_folder=self._read_optional_folder(self.horizontal_asset_folder_var),
            output_path=output_path,
            clip_start_min=clip_start_min,
            clip_start_max=clip_start_max,
            clip_end_min=clip_end_min,
            clip_end_max=clip_end_max,
            interval_min=interval_min,
            interval_max=interval_max,
            allow_reuse=asset_exhaustion_policy == "reuse",
            asset_exhaustion_policy="abort" if asset_exhaustion_policy == "no_reuse" else asset_exhaustion_policy,
            short_asset_policy=short_asset_policy,
            probe_workers=self._resolve_probe_worker_count(),
        )

    def _choose_asset_exhaustion_policy(self, settings: OverlaySettings) -> str:
        assets = scan_video_assets(settings.asset_folder)
        main_info = probe_video(settings.main_video)
        required = estimate_required_assets(main_info.duration, settings.clip_duration, settings.interval_min)
        if len(assets) >= required:
            return "no_reuse"

        message = (
            f"按最短间隔估算最多需要 {required} 个素材，当前只有 {len(assets)} 个。\n\n"
            "请选择处理方式。"
        )
        return self._choice_dialog(
            "素材可能不足",
            message,
            [
                ("重新打乱循环复用", "reuse"),
                ("用完后续就不插入了", "stop"),
                ("停止导出", "abort_export"),
            ],
        )

    def _choose_short_asset_policy(self, settings: OverlaySettings) -> str:
        assets = scan_video_assets(settings.asset_folder)
        short_assets: list[tuple[Path, float]] = []
        for asset in assets:
            info = probe_video(asset)
            if info.duration < settings.clip_end:
                short_assets.append((asset, info.duration))

        if not short_assets:
            return "error"

        first_asset, first_duration = short_assets[0]
        message = (
            f"素材时长不足：{first_asset.name} 只有 {first_duration:.2f} 秒，"
            f"无法截取到第 {settings.clip_end:.2f} 秒。\n\n"
            "选择继续时，该素材只截取到能取得的秒数。"
            "是否之后遇到这种情况也都这样处理？"
        )
        return self._choice_dialog(
            "素材时长不足",
            message,
            [
                ("继续，之后都自动裁短", "trim"),
                ("仅当前设置继续", "trim"),
                ("停止导出", "abort"),
            ],
        )

    def _preflight_worker(self, settings: OverlaySettings) -> None:
        try:
            self.message_queue.put(("progress", "正在校验授权..."))
            license_info = verify_export_permission(
                progress=lambda message: self.message_queue.put(("progress", message))
            )
            self.message_queue.put(("progress", f"授权校验通过：{license_info.owner}"))
            if self.cancel_event.is_set():
                self.message_queue.put(("cancelled", None))
                return

            self.message_queue.put(("progress", "正在检查视频和素材..."))
            assets = scan_video_assets(settings.asset_folder)
            if self.cancel_event.is_set():
                self.message_queue.put(("cancelled", None))
                return
            main_info = probe_video(settings.main_video)
            required = estimate_required_assets(main_info.duration, settings.clip_duration, settings.interval_min)

            usable_asset_count, unusable_assets, short_assets = self._probe_assets_for_preflight(
                settings,
                assets,
            )
            if self.cancel_event.is_set():
                self.message_queue.put(("cancelled", None))
                return

            payload = {
                "settings": settings,
                "main_duration": main_info.duration,
                "asset_count": usable_asset_count,
                "total_asset_count": len(assets),
                "required_assets": required,
                "unusable_assets": unusable_assets,
                "short_assets": short_assets,
            }
            self.message_queue.put(("preflight_done", payload))
        except LicenseError as exc:
            self.message_queue.put(("warning", exc))
        except Exception as exc:
            self.message_queue.put(("error", exc))

    def _probe_assets_for_preflight(
        self,
        settings: OverlaySettings,
        assets: list[Path],
    ) -> tuple[int, list[tuple[str, float]], list[tuple[str, float]]]:
        """
        并发检测素材时长并整理预检结果。

        Args:
            settings (OverlaySettings): 当前导出设置。
            assets (list[Path]): 需要检测的小视频素材列表。

        Returns:
            tuple[int, list[tuple[str, float]], list[tuple[str, float]]]:
            可用素材数量、无法截取素材列表、短素材列表。
        """
        usable_asset_count = 0
        unusable_assets: list[tuple[str, float]] = []
        short_assets: list[tuple[str, float]] = []
        completed_count = 0
        worker_count = max(1, settings.probe_workers)

        executor = ThreadPoolExecutor(max_workers=worker_count)
        try:
            pending_assets = iter(assets)
            future_by_asset = {}
            for _index in range(min(worker_count, len(assets))):
                asset = next(pending_assets)
                future_by_asset[executor.submit(probe_video, asset)] = asset

            while future_by_asset:
                if self.cancel_event.is_set():
                    return usable_asset_count, unusable_assets, short_assets

                done_futures, _pending_futures = wait(
                    future_by_asset,
                    return_when=FIRST_COMPLETED,
                )
                for future in done_futures:
                    asset = future_by_asset.pop(future)
                    info = future.result()
                    completed_count += 1
                    self.message_queue.put(
                        ("progress", f"检查素材时长：{completed_count}/{len(assets)}")
                    )
                    if info.duration <= settings.clip_start_min:
                        unusable_assets.append((asset.name, info.duration))
                    else:
                        usable_asset_count += 1
                        if info.duration < settings.clip_end_max:
                            short_assets.append((asset.name, info.duration))

                    if self.cancel_event.is_set():
                        return usable_asset_count, unusable_assets, short_assets
                    try:
                        next_asset = next(pending_assets)
                    except StopIteration:
                        continue
                    future_by_asset[executor.submit(probe_video, next_asset)] = next_asset
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        return usable_asset_count, unusable_assets, short_assets

    def _start_render_after_preflight(self, payload: dict) -> None:
        if self.cancel_event.is_set():
            self._set_running(False)
            self.progress_bar.stop()
            self.progress_bar.configure(mode="determinate", value=0)
            self.progress_var.set("已取消。")
            self._append_log("已取消。")
            return

        settings = payload["settings"]
        asset_count = int(payload["asset_count"])
        total_asset_count = int(payload["total_asset_count"])
        required = int(payload["required_assets"])
        unusable_assets = payload["unusable_assets"]
        short_assets = payload["short_assets"]

        if unusable_assets:
            first_name, first_duration = unusable_assets[0]
            message = (
                f"有 {len(unusable_assets)} 个素材短于或等于起始秒 {settings.clip_start:.2f}，"
                f"无法截取。\n\n"
                f"第一个：{first_name} 只有 {first_duration:.2f} 秒。\n\n"
                "选择继续时，这些素材会被跳过，不参与随机插入。"
            )
            unusable_policy = self._resolve_preflight_policy(
                configured_policy=self.unusable_asset_policy_var.get(),
                title="素材无法截取",
                message=message,
                choices=[
                    ("跳过这些素材继续", "skip"),
                    ("停止导出", "abort"),
                ],
            )
            if unusable_policy == "abort":
                self._stop_preflight_export("已按设置停止导出。")
                return
            self._append_log(f"跳过 {len(unusable_assets)} 个无法到达起始秒的素材。")

        asset_policy = "no_reuse"
        if asset_count < required:
            message = (
                f"按最短间隔估算最多需要 {required} 个素材，"
                f"当前可用素材 {asset_count} 个，总素材 {total_asset_count} 个。\n\n"
                "请选择处理方式。"
            )
            asset_policy = self._resolve_preflight_policy(
                configured_policy=self.asset_exhaustion_policy_var.get(),
                title="素材可能不足",
                message=message,
                choices=[
                    ("重新打乱循环复用", "reuse"),
                    ("用完后续就不插入了", "stop"),
                    ("停止导出", "abort_export"),
                ],
            )
            if asset_policy == "abort_export":
                self._stop_preflight_export("已按设置停止导出。")
                return

        short_policy = "error"
        if short_assets:
            first_name, first_duration = short_assets[0]
            message = (
                f"素材时长不足：{first_name} 只有 {first_duration:.2f} 秒，"
                f"无法截取到第 {settings.clip_end_max:.2f} 秒。\n\n"
                "选择继续时，该素材只截取到能取得的秒数。"
                "是否之后遇到这种情况也都这样处理？"
            )
            short_policy = self._resolve_preflight_policy(
                configured_policy=self.short_asset_policy_var.get(),
                title="素材时长不足",
                message=message,
                choices=[
                    ("继续，之后都自动裁短", "trim"),
                    ("仅当前设置继续", "trim"),
                    ("停止导出", "abort"),
                ],
            )
            if short_policy == "abort":
                self._stop_preflight_export("已按设置停止导出。")
                return

        settings = replace(
            settings,
            allow_reuse=asset_policy == "reuse",
            asset_exhaustion_policy="abort" if asset_policy == "no_reuse" else asset_policy,
            short_asset_policy=short_policy,
        )
        self.current_main_duration = float(payload["main_duration"])
        self._save_config()
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate", value=0)
        self._append_log("开始处理。")
        self.worker_thread = threading.Thread(
            target=self._worker,
            args=(settings,),
            daemon=True,
        )
        self.worker_thread.start()

    def _resolve_preflight_policy(
        self,
        configured_policy: str,
        title: str,
        message: str,
        choices: list[tuple[str, str]],
    ) -> str:
        """
        按设置决定导出前异常的处理方式。

        Args:
            configured_policy (str): 用户提前配置的处理策略。
            title (str): 需要询问时的弹窗标题。
            message (str): 需要询问时的弹窗内容。
            choices (list[tuple[str, str]]): 可选按钮文案和策略值。

        Returns:
            str: 最终使用的处理策略。
        """
        if configured_policy == "ask":
            return self._choice_dialog(title, message, choices)
        return configured_policy

    def _stop_preflight_export(self, log_message: str) -> None:
        """
        在预检阶段停止导出并恢复界面状态。

        Args:
            log_message (str): 写入日志区域的停止原因。

        Returns:
            None: 无返回值。
        """
        self._set_running(False)
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate", value=0)
        self.progress_var.set("已停止。")
        self._append_log(log_message)

    def _worker(self, settings: OverlaySettings) -> None:
        try:
            events = render_overlay_video(
                settings,
                progress=lambda message: self.message_queue.put(("progress", message)),
                cancel_event=self.cancel_event,
            )
        except Exception as exc:
            self.message_queue.put(("error", exc))
        else:
            self.message_queue.put(("done", events))

    def _cancel_export(self) -> None:
        self.cancel_event.set()
        self.progress_var.set("正在取消...")

    def _poll_worker_messages(self) -> None:
        try:
            while True:
                kind, payload = self.message_queue.get_nowait()
                if kind == "progress":
                    message = str(payload)
                    self.progress_var.set(message)
                    self._update_progress_bar(message)
                    self._append_log(message)
                elif kind == "error":
                    self._set_running(False)
                    self.progress_bar.stop()
                    message = str(payload)
                    self.progress_var.set("导出失败。")
                    self.progress_bar.configure(mode="determinate", value=0)
                    self._append_log(message)
                    self._show_error("导出失败", message)
                elif kind == "warning":
                    self._set_running(False)
                    self.progress_bar.stop()
                    message = str(payload)
                    self.progress_var.set("授权校验失败。")
                    self.progress_bar.configure(mode="determinate", value=0)
                    self._append_log(message)
                    self._show_warning("警告", message)
                elif kind == "cancelled":
                    self._set_running(False)
                    self.progress_bar.stop()
                    self.progress_bar.configure(mode="determinate", value=0)
                    self.progress_var.set("已取消。")
                    self._append_log("已取消。")
                elif kind == "preflight_done":
                    self.progress_bar.stop()
                    self.progress_bar.configure(mode="determinate", value=0)
                    self._start_render_after_preflight(payload)
                elif kind == "done":
                    self._set_running(False)
                    self.progress_bar.stop()
                    count = len(payload) if hasattr(payload, "__len__") else 0
                    self.progress_var.set("导出完成。")
                    self.progress_bar.configure(mode="determinate", value=100)
                    self._append_log(f"导出完成，共插入 {count} 段。")
                    self._show_info("完成", f"导出完成，共插入 {count} 段。")
        except queue.Empty:
            pass
        self.after(100, self._poll_worker_messages)

    def _set_running(self, running: bool) -> None:
        self.start_button.configure(state=tk.DISABLED if running else tk.NORMAL)
        self.cancel_button.configure(state=tk.NORMAL if running else tk.DISABLED)
        self.settings_button.configure(state=tk.DISABLED if running else tk.NORMAL)

    def _is_busy(self) -> bool:
        return bool(
            (self.worker_thread and self.worker_thread.is_alive())
            or (self.preflight_thread and self.preflight_thread.is_alive())
        )

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _clear_log(self) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _bring_window_to_front(self) -> None:
        """
        恢复并置前主窗口，避免最小化时模态弹窗被藏住。

        Returns:
            None: 无返回值。
        """
        try:
            self.deiconify()
            self.state("normal")
            self.lift()
            self.focus_force()
            self.attributes("-topmost", True)
            self.after(200, lambda: self.attributes("-topmost", False))
        except tk.TclError:
            return

    def _show_info(self, title: str, message: str) -> None:
        """
        显示信息提示，并确保主窗口可见。

        Args:
            title (str): 弹窗标题。
            message (str): 弹窗内容。

        Returns:
            None: 无返回值。
        """
        self._bring_window_to_front()
        messagebox.showinfo(title, message, parent=self)

    def _show_warning(self, title: str, message: str) -> None:
        """
        显示警告提示，并确保主窗口可见。

        Args:
            title (str): 弹窗标题。
            message (str): 弹窗内容。

        Returns:
            None: 无返回值。
        """
        self._bring_window_to_front()
        messagebox.showwarning(title, message, parent=self)

    def _show_error(self, title: str, message: str) -> None:
        """
        显示错误提示，并确保主窗口可见。

        Args:
            title (str): 弹窗标题。
            message (str): 弹窗内容。

        Returns:
            None: 无返回值。
        """
        self._bring_window_to_front()
        messagebox.showerror(title, message, parent=self)

    def _choice_dialog(self, title: str, message: str, choices: list[tuple[str, str]]) -> str:
        self._bring_window_to_front()
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.transient(self)
        dialog.grab_set()
        dialog.resizable(False, False)

        result = {"value": choices[-1][1]}
        frame = ttk.Frame(dialog, padding=18)
        frame.grid(row=0, column=0, sticky="nsew")
        ttk.Label(frame, text=message, wraplength=460, justify=tk.LEFT).grid(row=0, column=0, columnspan=len(choices), sticky="w")

        def choose(value: str) -> None:
            result["value"] = value
            dialog.destroy()

        for index, (label, value) in enumerate(choices):
            ttk.Button(frame, text=label, command=lambda selected=value: choose(selected)).grid(
                row=1,
                column=index,
                padx=(0 if index == 0 else 8, 0),
                pady=(16, 0),
                sticky="ew",
            )
            frame.columnconfigure(index, weight=1)

        dialog.protocol("WM_DELETE_WINDOW", lambda: choose(choices[-1][1]))
        dialog.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - dialog.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dialog.winfo_height()) // 2
        dialog.geometry(f"+{max(0, x)}+{max(0, y)}")
        dialog.lift()
        dialog.focus_force()
        self.wait_window(dialog)
        return str(result["value"])

    def _update_progress_bar(self, message: str) -> None:
        if self.current_main_duration <= 0:
            return
        match = re.search(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", message)
        if not match:
            return
        seconds = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
        percent = max(0.0, min(100.0, seconds / self.current_main_duration * 100))
        self.progress_bar.configure(value=percent)

    def _open_output_folder(self) -> None:
        output_text = self._var_value(self.output_path_var)
        if not output_text:
            self._show_info("提示", "请先选择输出文件。")
            return
        folder = Path(output_text).expanduser().parent
        if not folder.exists():
            self._show_info("提示", "输出文件夹不存在。")
            return
        os.startfile(folder)

    def _open_selected_path(self, variable: tk.StringVar, path_kind: str) -> None:
        """
        打开路径输入框对应的文件或文件夹。

        Args:
            variable (tk.StringVar): 路径输入框变量。
            path_kind (str): 路径类型，支持 file、folder、output。

        Returns:
            None: 无返回值。
        """
        path_text = self._var_value(variable)
        if not path_text:
            self._show_info("提示", "请先选择路径。")
            return

        path = Path(path_text).expanduser()
        if path_kind == "output" and not path.exists():
            self._open_output_parent(path)
            return

        if path_kind == "folder" and not path.is_dir():
            self._show_info("提示", "文件夹不存在。")
            return

        if path_kind == "file" and not path.is_file():
            self._show_info("提示", "文件不存在。")
            return

        if not path.exists():
            self._show_info("提示", "路径不存在。")
            return

        os.startfile(path)

    def _open_output_parent(self, output_path: Path) -> None:
        """
        输出文件不存在时打开其所在文件夹。

        Args:
            output_path (Path): 输出文件路径。

        Returns:
            None: 无返回值。
        """
        folder = output_path.parent
        if not folder.exists():
            self._show_info("提示", "输出文件夹不存在。")
            return
        os.startfile(folder)

    def _copy_machine_code(self) -> None:
        machine_code = get_machine_code()
        self.clipboard_clear()
        self.clipboard_append(machine_code)
        self.update()
        self._show_info("机器码", f"机器码已复制：\n{machine_code}")

    def _show_settings(self) -> None:
        """
        打开设置窗口，配置导出前异常处理策略。

        Returns:
            None: 无返回值。
        """
        dialog = tk.Toplevel(self)
        dialog.title("设置")
        dialog.geometry("560x320")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()

        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(1, weight=1)

        unusable_policy_label = tk.StringVar(
            value=self._policy_label(
                UNUSABLE_ASSET_POLICY_OPTIONS,
                self.unusable_asset_policy_var.get(),
            )
        )
        asset_policy_label = tk.StringVar(
            value=self._policy_label(
                ASSET_EXHAUSTION_POLICY_OPTIONS,
                self.asset_exhaustion_policy_var.get(),
            )
        )
        short_policy_label = tk.StringVar(
            value=self._policy_label(
                SHORT_ASSET_POLICY_OPTIONS,
                self.short_asset_policy_var.get(),
            )
        )
        probe_worker_label = tk.StringVar(
            value=self._policy_label(
                PROBE_WORKER_OPTIONS,
                self.probe_worker_var.get(),
            )
        )

        self._add_policy_row(
            frame,
            0,
            "素材无法到达起始秒",
            unusable_policy_label,
            UNUSABLE_ASSET_POLICY_OPTIONS,
        )
        self._add_policy_row(
            frame,
            1,
            "素材数量可能不足",
            asset_policy_label,
            ASSET_EXHAUSTION_POLICY_OPTIONS,
        )
        self._add_policy_row(
            frame,
            2,
            "素材不到结束秒",
            short_policy_label,
            SHORT_ASSET_POLICY_OPTIONS,
        )
        self._add_policy_row(
            frame,
            3,
            "素材检测线程数",
            probe_worker_label,
            PROBE_WORKER_OPTIONS,
        )

        hint = "选择“导出时询问”会保持原来的弹窗确认流程；线程数选“自动”会按电脑配置取 2-8。"
        ttk.Label(frame, text=hint, wraplength=500).grid(row=4, column=0, columnspan=2, sticky="w", pady=(14, 0))

        button_frame = ttk.Frame(frame)
        button_frame.grid(row=5, column=0, columnspan=2, sticky="e", pady=(22, 0))

        def save_settings() -> None:
            """
            保存设置窗口中的策略选项。

            Returns:
                None: 无返回值。
            """
            self.unusable_asset_policy_var.set(
                self._policy_value(UNUSABLE_ASSET_POLICY_OPTIONS, unusable_policy_label.get())
            )
            self.asset_exhaustion_policy_var.set(
                self._policy_value(ASSET_EXHAUSTION_POLICY_OPTIONS, asset_policy_label.get())
            )
            self.short_asset_policy_var.set(
                self._policy_value(SHORT_ASSET_POLICY_OPTIONS, short_policy_label.get())
            )
            self.probe_worker_var.set(
                self._policy_value(PROBE_WORKER_OPTIONS, probe_worker_label.get())
            )
            self._save_config()
            dialog.destroy()

        ttk.Button(button_frame, text="保存", command=save_settings).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(button_frame, text="取消", command=dialog.destroy).grid(row=0, column=1)

    def _add_policy_row(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
        options: dict[str, str],
    ) -> None:
        """
        在设置窗口中添加一行策略下拉框。

        Args:
            parent (ttk.Frame): 父级容器。
            row (int): 所在行号。
            label (str): 左侧说明文字。
            variable (tk.StringVar): 下拉框显示变量。
            options (dict[str, str]): 显示文字到策略值的映射。

        Returns:
            None: 无返回值。
        """
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=8)
        combobox = ttk.Combobox(
            parent,
            textvariable=variable,
            values=list(options.keys()),
            state="readonly",
            width=28,
        )
        combobox.grid(row=row, column=1, sticky="ew", padx=(16, 0), pady=8)

    def _policy_label(self, options: dict[str, str], value: str) -> str:
        """
        根据策略值取得设置窗口中的显示文字。

        Args:
            options (dict[str, str]): 显示文字到策略值的映射。
            value (str): 当前策略值。

        Returns:
            str: 对应显示文字，未知值返回第一个选项。
        """
        for label, option_value in options.items():
            if option_value == value:
                return label
        return next(iter(options))

    def _policy_value(self, options: dict[str, str], label: str) -> str:
        """
        根据设置窗口显示文字取得策略值。

        Args:
            options (dict[str, str]): 显示文字到策略值的映射。
            label (str): 下拉框显示文字。

        Returns:
            str: 对应策略值，未知文字返回第一个策略值。
        """
        return options.get(label, next(iter(options.values())))

    def _show_help(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("使用说明")
        dialog.geometry("680x560")
        dialog.minsize(620, 480)
        dialog.transient(self)

        frame = ttk.Frame(dialog, padding=14)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        text = tk.Text(frame, wrap=tk.WORD, height=20)
        text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        text.configure(yscrollcommand=scrollbar.set)

        text.insert(tk.END, self._help_text())
        text.configure(state=tk.DISABLED)

        ttk.Button(frame, text="关闭", command=dialog.destroy).grid(row=1, column=0, columnspan=2, sticky="e", pady=(12, 0))

    def _help_text(self) -> str:
        return (
            "AutoOverlay 使用说明\n\n"
            "基本流程\n"
            "1. 选择主视频。\n"
            "2. 选择小视频素材文件夹。\n"
            "3. 设置小视频截取片段，例如起始秒 5、结束秒 8，表示每个素材取第 5 秒到第 8 秒。\n"
            "   起始秒和结束秒都支持最小值、最大值；填相同数字就是固定截取。\n"
            "   结束最小秒必须大于起始最大秒。\n"
            "4. 设置随机插入间隔，例如 5-10 秒，表示每段素材结束后随机等待 5 到 10 秒再插入下一段。\n"
            "5. 选择输出文件，点击开始导出。\n\n"
            "参数说明\n"
            "- 主视频：最终导出的基础视频，导出会保留主视频原声音。\n"
            "- 素材文件夹：存放小视频素材的文件夹。\n"
            "- 横屏素材文件夹：可留空。不为空时，会随机选取其中素材连续叠加到视频底部，直到素材用完或主视频结束。\n"
            "- 起始最小秒 / 起始最大秒：每段小视频随机截取起点的范围。\n"
            "- 结束最小秒 / 结束最大秒：每段小视频随机截取终点的范围，结束最小秒必须大于起始最大秒。\n"
            "- 最小秒 / 最大秒：两次插入之间的等待时间，最大秒必须大于或等于最小秒。\n"
            "- 输出文件：导出 MP4 文件路径，不要和主视频相同。\n\n"
            "间隔示例\n"
            "- 0 - 0：一个素材接一个素材，中间不等待。\n"
            "- 1 - 1：固定间隔 1 秒。\n"
            "- 2 - 2：固定间隔 2 秒。\n"
            "- 5 - 10：每次随机等待 5 到 10 秒。\n\n"
            "素材处理规则\n"
            "- 小视频会自动静音，只保留主视频声音。\n"
            "- 小视频会等比例缩放并居中裁剪到主视频分辨率，避免拉伸变形。\n"
            "- 横屏素材不会缩放和裁剪，只叠加到画面最下方。\n"
            "- 默认同一轮处理不重复使用同一个素材。\n"
            "- 素材不足时可选择重新打乱循环复用、用完后不再插入，或停止导出。\n"
            "- 如果素材能到达起始秒但不到结束秒，可以选择裁到可用结尾。\n"
            "- 如果素材时长小于或等于起始秒，该素材无法截取，只能跳过或停止。\n\n"
            "设置说明\n"
            "- 点击“设置”可提前配置以上异常情况的处理方式。\n"
            "- 选择“导出时询问”会保持原来的弹窗确认流程。\n\n"
            "- 素材检测线程数默认自动，软件会根据电脑配置选择 2 到 8 个线程。\n\n"
            "授权说明\n"
            "- 点击开始导出时会先校验本机授权文件和远程接口。\n"
            "- 授权文件可放在 AutoOverlay.exe 同目录，或 %APPDATA%\\AutoOverlay。\n"
            "- 点击“复制机器码”可复制当前设备机器码，用于生成授权文件。\n\n"
            "其他\n"
            "- 点击取消会停止当前检测或导出。\n"
            "- 点击打开输出文件夹可快速查看导出结果。\n"
            "- 软件会自动记住上次选择的路径和参数。\n"
        )

    def _get_config_path(self) -> Path:
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / "AutoOverlay" / "config.json"
        return Path.home() / ".autooverlay" / "config.json"

    def _load_config(self) -> None:
        """
        读取上次保存的界面配置。

        Returns:
            None: 无返回值。
        """
        if not self.config_path.exists():
            return
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.main_video_var.set(str(data.get("main_video", "")))
        self.asset_folder_var.set(str(data.get("asset_folder", "")))
        self.horizontal_asset_folder_var.set(str(data.get("horizontal_asset_folder", "")))
        self.output_path_var.set(str(data.get("output_path", "")))
        old_clip_start = data.get("clip_start", self.clip_start_min_var.get())
        old_clip_end = data.get("clip_end", self.clip_end_min_var.get())
        self.clip_start_min_var.set(str(data.get("clip_start_min", old_clip_start)))
        self.clip_start_max_var.set(str(data.get("clip_start_max", old_clip_start)))
        self.clip_end_min_var.set(str(data.get("clip_end_min", old_clip_end)))
        self.clip_end_max_var.set(str(data.get("clip_end_max", old_clip_end)))
        self.interval_min_var.set(str(data.get("interval_min", self.interval_min_var.get())))
        self.interval_max_var.set(str(data.get("interval_max", self.interval_max_var.get())))
        self.unusable_asset_policy_var.set(
            self._valid_policy_value(
                str(data.get("unusable_asset_policy", self.unusable_asset_policy_var.get())),
                UNUSABLE_ASSET_POLICY_OPTIONS,
            )
        )
        self.asset_exhaustion_policy_var.set(
            self._valid_policy_value(
                str(data.get("asset_exhaustion_policy", self.asset_exhaustion_policy_var.get())),
                ASSET_EXHAUSTION_POLICY_OPTIONS,
            )
        )
        self.short_asset_policy_var.set(
            self._valid_policy_value(
                str(data.get("short_asset_policy", self.short_asset_policy_var.get())),
                SHORT_ASSET_POLICY_OPTIONS,
            )
        )
        self.probe_worker_var.set(
            self._valid_probe_worker_value(
                str(data.get("probe_workers", self.probe_worker_var.get()))
            )
        )

    def _save_config(self) -> None:
        """
        保存当前界面配置。

        Returns:
            None: 无返回值。
        """
        data = {
            "main_video": self._var_value(self.main_video_var),
            "asset_folder": self._var_value(self.asset_folder_var),
            "horizontal_asset_folder": self._var_value(self.horizontal_asset_folder_var),
            "output_path": self._var_value(self.output_path_var),
            "clip_start_min": self.clip_start_min_var.get().strip(),
            "clip_start_max": self.clip_start_max_var.get().strip(),
            "clip_end_min": self.clip_end_min_var.get().strip(),
            "clip_end_max": self.clip_end_max_var.get().strip(),
            "interval_min": self.interval_min_var.get().strip(),
            "interval_max": self.interval_max_var.get().strip(),
            "unusable_asset_policy": self.unusable_asset_policy_var.get().strip(),
            "asset_exhaustion_policy": self.asset_exhaustion_policy_var.get().strip(),
            "short_asset_policy": self.short_asset_policy_var.get().strip(),
            "probe_workers": self.probe_worker_var.get().strip(),
        }
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _valid_policy_value(self, value: str, options: dict[str, str]) -> str:
        """
        读取配置时过滤未知策略值。

        Args:
            value (str): 配置文件中的策略值。
            options (dict[str, str]): 允许的策略选项。

        Returns:
            str: 有效策略值，未知值返回 ask。
        """
        if value in options.values():
            return value
        return "ask"

    def _valid_probe_worker_value(self, value: str) -> str:
        """
        读取配置时过滤未知的素材检测线程数。

        Args:
            value (str): 配置文件中的线程数设置。

        Returns:
            str: 有效线程数设置，未知值返回 auto。
        """
        if value in PROBE_WORKER_OPTIONS.values():
            return value
        return "auto"

    def _resolve_probe_worker_count(self) -> int:
        """
        根据当前设置计算实际素材检测线程数。

        Returns:
            int: 实际使用的线程数。
        """
        value = self._valid_probe_worker_value(self.probe_worker_var.get().strip())
        if value != "auto":
            return max(1, int(value))

        cpu_count = os.cpu_count() or 2
        return min(MAX_AUTO_PROBE_WORKERS, max(2, cpu_count // 2))

    def _var_value(self, variable: tk.StringVar) -> str:
        value = variable.get().strip()
        placeholder = self.placeholders.get(str(variable))
        if placeholder and value == placeholder:
            return ""
        return value

    def _read_optional_folder(self, variable: tk.StringVar) -> Path | None:
        """
        从可留空的文件夹输入框读取路径。

        Args:
            variable (tk.StringVar): 文件夹输入框变量。

        Returns:
            Path | None: 有内容时返回路径，留空时返回 None。
        """
        value = self._var_value(variable)
        if not value:
            return None
        return Path(value)

    def _apply_placeholder(self, entry: tk.Entry, variable: tk.StringVar) -> None:
        if variable.get().strip():
            if variable.get() == self.placeholders.get(str(variable)):
                entry.configure(fg="#777777")
            else:
                entry.configure(fg="black")
            return
        placeholder = self.placeholders.get(str(variable))
        if placeholder:
            variable.set(placeholder)
            entry.configure(fg="#777777")

    def _clear_placeholder(self, entry: tk.Entry, variable: tk.StringVar) -> None:
        placeholder = self.placeholders.get(str(variable))
        if placeholder and variable.get() == placeholder:
            variable.set("")
        entry.configure(fg="black")

    def _on_close(self) -> None:
        self._save_config()
        if self._is_busy():
            self.cancel_event.set()
        self.destroy()


if __name__ == "__main__":
    app = AutoOverlayApp()
    app.mainloop()
