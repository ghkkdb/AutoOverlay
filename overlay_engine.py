from __future__ import annotations

import math
import os
import random
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import imageio_ffmpeg


VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
BOUNDARY_PAD_SECONDS = 0.12
TIME_EPSILON = 0.000001


class OverlayError(RuntimeError):
    """Raised for user-facing processing errors."""


@dataclass(frozen=True)
class VideoInfo:
    path: Path
    duration: float
    width: int
    height: int
    has_audio: bool


@dataclass(frozen=True)
class InsertEvent:
    asset_path: Path
    start_time: float
    duration: float
    clip_start: float

    @property
    def end_time(self) -> float:
        return self.start_time + self.duration


@dataclass(frozen=True)
class HorizontalEvent:
    asset_path: Path
    start_time: float
    duration: float

    @property
    def end_time(self) -> float:
        return self.start_time + self.duration


@dataclass(frozen=True)
class OverlaySettings:
    main_video: Path
    asset_folder: Path
    horizontal_asset_folder: Path | None
    output_path: Path
    clip_start_min: float
    clip_start_max: float
    clip_end_min: float
    clip_end_max: float
    interval_min: float
    interval_max: float
    allow_reuse: bool = False
    asset_exhaustion_policy: str = "abort"
    short_asset_policy: str = "error"
    probe_workers: int = 1

    @property
    def clip_start(self) -> float:
        return self.clip_start_min

    @property
    def clip_end(self) -> float:
        return self.clip_end_max

    @property
    def clip_duration(self) -> float:
        return self.clip_end_min - self.clip_start_max


ProgressCallback = Callable[[str], None]


def get_ffmpeg_exe() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def scan_video_assets(folder: Path) -> list[Path]:
    if not folder.exists() or not folder.is_dir():
        raise OverlayError("小视频素材文件夹不存在。")

    assets = [
        item
        for item in folder.iterdir()
        if item.is_file() and item.suffix.lower() in VIDEO_EXTENSIONS
    ]
    assets.sort(key=lambda path: path.name.lower())
    return assets


def probe_video(path: Path) -> VideoInfo:
    if not path.exists() or not path.is_file():
        raise OverlayError(f"视频文件不存在：{path}")

    cmd = [get_ffmpeg_exe(), "-hide_banner", "-i", str(path)]
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_creationflags(),
    )
    text = f"{result.stdout}\n{result.stderr}"

    duration = _parse_duration(text)
    width, height = _parse_video_size(text)
    has_audio = bool(re.search(r"Stream #\d+:\d+.*Audio:", text))

    if duration <= 0:
        raise OverlayError(f"无法读取视频时长：{path}")
    if width <= 0 or height <= 0:
        raise OverlayError(f"无法读取视频分辨率：{path}")

    return VideoInfo(path=path, duration=duration, width=width, height=height, has_audio=has_audio)


def validate_settings(settings: OverlaySettings) -> None:
    """
    校验导出参数是否满足基础处理要求。

    Args:
        settings (OverlaySettings): 导出参数。

    Returns:
        None: 无返回值。

    Raises:
        OverlayError: 参数不合法时抛出。
    """
    if settings.clip_start_min < 0 or settings.clip_start_max < 0:
        raise OverlayError("小视频截取起始时间不能小于 0。")
    if settings.clip_end_min < 0 or settings.clip_end_max < 0:
        raise OverlayError("小视频截取结束时间不能小于 0。")
    if settings.clip_start_max < settings.clip_start_min:
        raise OverlayError("小视频截取起始最大秒必须大于或等于起始最小秒。")
    if settings.clip_end_max < settings.clip_end_min:
        raise OverlayError("小视频截取结束最大秒必须大于或等于结束最小秒。")
    if settings.clip_end_min <= settings.clip_start_max:
        raise OverlayError("小视频截取结束最小秒必须大于起始最大秒。")
    if settings.interval_min < 0 or settings.interval_max < 0:
        raise OverlayError("随机间隔不能小于 0。")
    if settings.interval_max < settings.interval_min:
        raise OverlayError("随机间隔最大值必须大于或等于最小值。")
    if not settings.main_video.exists() or not settings.main_video.is_file():
        raise OverlayError("请选择有效的主视频文件。")
    if not settings.asset_folder.exists() or not settings.asset_folder.is_dir():
        raise OverlayError("请选择有效的小视频素材文件夹。")
    if settings.horizontal_asset_folder and (
        not settings.horizontal_asset_folder.exists()
        or not settings.horizontal_asset_folder.is_dir()
    ):
        raise OverlayError("请选择有效的横屏素材文件夹，或留空不使用。")
    if settings.output_path.suffix.lower() != ".mp4":
        raise OverlayError("输出文件建议使用 .mp4 扩展名。")
    if settings.output_path.resolve() == settings.main_video.resolve():
        raise OverlayError("输出文件不能覆盖主视频文件。")


def build_insert_schedule(
    main_duration: float,
    assets: list[Path],
    clip_duration: float,
    interval_min: float,
    interval_max: float,
    allow_reuse: bool,
    asset_exhaustion_policy: str = "abort",
    rng: random.Random | None = None,
    settings: OverlaySettings | None = None,
    duration_by_asset: dict[Path, float] | None = None,
) -> tuple[list[InsertEvent], bool]:
    """
    生成小视频随机插入计划。

    Args:
        main_duration (float): 主视频总时长。
        assets (list[Path]): 可用小视频素材列表。
        clip_duration (float): 固定截取时长，未传入 settings 时使用。
        interval_min (float): 插入间隔最小秒数。
        interval_max (float): 插入间隔最大秒数。
        allow_reuse (bool): 素材用完后是否允许复用。
        asset_exhaustion_policy (str): 素材不足时的处理策略。
        rng (random.Random | None): 随机数生成器。
        settings (OverlaySettings | None): 随机截取区间设置。
        duration_by_asset (dict[Path, float] | None): 已探测的素材时长。

    Returns:
        tuple[list[InsertEvent], bool]: 插入事件列表，以及是否发生过素材复用。
    """
    if not assets:
        raise OverlayError("素材文件夹中没有可用视频文件。")
    if clip_duration <= 0:
        raise OverlayError("小视频截取时长必须大于 0。")

    rng = rng or random.Random()
    events: list[InsertEvent] = []
    pool = assets[:]
    rng.shuffle(pool)
    reused = False
    cursor = 0.0

    while True:
        cursor += rng.uniform(interval_min, interval_max)
        remaining_duration = main_duration - cursor
        if remaining_duration <= TIME_EPSILON:
            break
        if cursor + clip_duration > main_duration and not _is_continuous_overlay(interval_min, interval_max):
            break

        if not pool:
            if allow_reuse or asset_exhaustion_policy == "reuse":
                pool = assets[:]
                rng.shuffle(pool)
                reused = True
            elif asset_exhaustion_policy == "stop":
                break
            else:
                break

        asset_path = pool.pop()
        clip_start, current_duration = _choose_clip_window(
            settings,
            clip_duration,
            asset_path,
            duration_by_asset,
            rng,
        )
        if current_duration <= 0:
            continue
        if cursor + current_duration > main_duration:
            if not _is_continuous_overlay(interval_min, interval_max):
                break
            current_duration = remaining_duration
        events.append(
            InsertEvent(
                asset_path=asset_path,
                start_time=cursor,
                duration=current_duration,
                clip_start=clip_start,
            )
        )
        cursor += current_duration

    return events, reused


def build_horizontal_schedule(
    main_duration: float,
    assets: list[Path],
    duration_by_asset: dict[Path, float],
    rng: random.Random | None = None,
) -> list[HorizontalEvent]:
    """
    生成横屏素材从头到尾连续叠加到底部的计划。

    Args:
        main_duration (float): 主视频总时长。
        assets (list[Path]): 横屏素材列表。
        duration_by_asset (dict[Path, float]): 横屏素材时长表。
        rng (random.Random | None): 随机数生成器。

    Returns:
        list[HorizontalEvent]: 横屏素材叠加事件列表。
    """
    rng = rng or random.Random()
    pool = assets[:]
    rng.shuffle(pool)
    cursor = 0.0
    events: list[HorizontalEvent] = []

    while pool and cursor < main_duration:
        asset_path = pool.pop()
        duration = min(duration_by_asset[asset_path], main_duration - cursor)
        if duration <= 0:
            continue
        events.append(
            HorizontalEvent(
                asset_path=asset_path,
                start_time=cursor,
                duration=duration,
            )
        )
        cursor += duration

    return events


def estimate_required_assets(
    main_duration: float,
    clip_duration: float,
    interval_min: float,
) -> int:
    if clip_duration <= 0:
        return 0
    interval = max(0.0, interval_min)
    cursor = 0.0
    count = 0
    while True:
        cursor += interval
        if cursor + clip_duration > main_duration:
            if interval <= TIME_EPSILON and main_duration - cursor > TIME_EPSILON:
                return count + 1
            return count
        count += 1
        cursor += clip_duration


def _is_continuous_overlay(interval_min: float, interval_max: float) -> bool:
    """
    判断当前间隔设置是否表示小视频连续覆盖主视频。

    Args:
        interval_min (float): 插入间隔最小秒数。
        interval_max (float): 插入间隔最大秒数。

    Returns:
        bool: 最小和最大间隔都为 0 时返回 True。
    """
    return interval_min <= TIME_EPSILON and interval_max <= TIME_EPSILON


def render_overlay_video(
    settings: OverlaySettings,
    progress: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> list[InsertEvent]:
    validate_settings(settings)
    progress = progress or (lambda _message: None)
    cancel_event = cancel_event or threading.Event()

    assets = scan_video_assets(settings.asset_folder)
    if not assets:
        raise OverlayError("素材文件夹中没有可用视频文件。")

    progress("读取主视频信息...")
    main_info = probe_video(settings.main_video)
    assets, duration_by_asset = _filter_usable_assets(settings, assets, progress)
    if not assets:
        raise OverlayError(
            f"没有素材能从第 {settings.clip_start:.2f} 秒开始截取。"
        )
    required_assets = estimate_required_assets(
        main_info.duration,
        settings.clip_duration,
        settings.interval_min,
    )
    if settings.asset_exhaustion_policy == "abort" and not settings.allow_reuse and len(assets) < required_assets:
        raise OverlayError(
            f"素材数量可能不足。按最短间隔估算最多需要 {required_assets} 个素材，"
            f"当前只有 {len(assets)} 个。请允许循环复用或增加素材。"
        )

    events, _reused = build_insert_schedule(
        main_duration=main_info.duration,
        assets=assets,
        clip_duration=settings.clip_duration,
        interval_min=settings.interval_min,
        interval_max=settings.interval_max,
        allow_reuse=settings.allow_reuse,
        asset_exhaustion_policy=settings.asset_exhaustion_policy,
        settings=settings,
        duration_by_asset=duration_by_asset,
    )
    if not events:
        raise OverlayError("按当前间隔和截取时长没有可插入的小视频片段。")

    events = _normalize_event_assets(settings, events, progress, duration_by_asset)
    horizontal_events = _build_horizontal_events(settings, main_info.duration, progress)

    progress(f"生成插入计划：{len(events)} 段。")
    if horizontal_events:
        progress(f"生成横屏素材计划：{len(horizontal_events)} 段。")
    settings.output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = build_ffmpeg_command(settings, main_info, events, horizontal_events)
    progress("开始导出视频...")
    _run_ffmpeg(cmd, progress, cancel_event)
    progress("导出完成。")
    return events


def _normalize_event_assets(
    settings: OverlaySettings,
    events: list[InsertEvent],
    progress: ProgressCallback,
    known_durations: dict[Path, float] | None = None,
) -> list[InsertEvent]:
    unique_assets = sorted({event.asset_path for event in events}, key=lambda path: path.name.lower())
    progress("检查小视频素材时长...")
    duration_by_asset: dict[Path, float] = dict(known_durations or {})
    missing_assets = [asset_path for asset_path in unique_assets if asset_path not in duration_by_asset]
    if missing_assets:
        for asset_path, info in _probe_videos(missing_assets, settings.probe_workers):
            duration_by_asset[asset_path] = info.duration

    for asset_path in unique_assets:
        asset_duration = duration_by_asset[asset_path]
        if asset_duration <= settings.clip_start_min:
            raise OverlayError(
                f"素材时长不足：{asset_path.name} 只有 {asset_duration:.2f} 秒，"
                f"无法从第 {settings.clip_start_min:.2f} 秒开始截取。"
            )
        if asset_duration < settings.clip_end_max:
            if settings.short_asset_policy == "trim":
                progress(
                    f"素材较短，自动裁到可用时长：{asset_path.name} "
                    f"最多裁到 {asset_duration:.2f} 秒。"
                )
                continue
            raise OverlayError(
                f"素材时长不足：{asset_path.name} 只有 {asset_duration:.2f} 秒，"
                f"无法截取到第 {settings.clip_end_max:.2f} 秒。"
            )

    normalized: list[InsertEvent] = []
    for event in events:
        asset_duration = duration_by_asset[event.asset_path]
        clip_duration = min(event.duration, max(0.0, asset_duration - event.clip_start))
        if clip_duration <= 0:
            continue
        normalized.append(
            InsertEvent(
                asset_path=event.asset_path,
                start_time=event.start_time,
                duration=clip_duration,
                clip_start=event.clip_start,
            )
        )
    if not normalized:
        raise OverlayError("所有计划插入的素材都没有可截取片段。")
    return normalized


def _filter_usable_assets(
    settings: OverlaySettings,
    assets: list[Path],
    progress: ProgressCallback,
) -> tuple[list[Path], dict[Path, float]]:
    progress("检查素材是否能到达起始秒...")
    usable_assets: list[Path] = []
    duration_by_asset: dict[Path, float] = {}
    for asset_path, info in _probe_videos(assets, settings.probe_workers):
        duration_by_asset[asset_path] = info.duration
        if info.duration <= settings.clip_start_min:
            progress(
                f"跳过无法截取的素材：{asset_path.name} "
                f"只有 {info.duration:.2f} 秒，起始最小秒为 {settings.clip_start_min:.2f}。"
            )
            continue
        usable_assets.append(asset_path)
    return usable_assets, duration_by_asset


def build_ffmpeg_command(
    settings: OverlaySettings,
    main_info: VideoInfo,
    events: Iterable[InsertEvent],
    horizontal_events: Iterable[HorizontalEvent] | None = None,
) -> list[str]:
    """
    根据插入计划生成 FFmpeg 命令。

    Args:
        settings (OverlaySettings): 导出设置。
        main_info (VideoInfo): 主视频信息。
        events (Iterable[InsertEvent]): 小视频插入事件。
        horizontal_events (Iterable[HorizontalEvent] | None): 横屏素材叠加事件。

    Returns:
        list[str]: 可直接传给 subprocess 的 FFmpeg 命令参数。
    """
    event_list = list(events)
    horizontal_event_list = list(horizontal_events or [])
    cmd = [
        get_ffmpeg_exe(),
        "-y",
        "-hide_banner",
        "-i",
        str(settings.main_video),
    ]

    for event in event_list:
        cmd.extend([
            "-ss",
            _fmt_time(event.clip_start),
            "-t",
            _fmt_time(event.duration),
            "-i",
            str(event.asset_path),
        ])

    for event in horizontal_event_list:
        cmd.extend(["-i", str(event.asset_path)])

    filter_complex = _build_filter_complex(main_info, event_list, horizontal_event_list)
    output_label = f"[v{len(event_list) + len(horizontal_event_list)}]"
    cmd.extend(["-filter_complex", filter_complex, "-map", output_label])

    if main_info.has_audio:
        cmd.extend(["-map", "0:a?", "-c:a", "copy"])
    else:
        cmd.extend(["-an"])

    cmd.extend([
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-t",
        _fmt_time(main_info.duration),
        str(settings.output_path),
    ])
    return cmd


def _build_filter_complex(
    main_info: VideoInfo,
    events: list[InsertEvent],
    horizontal_events: list[HorizontalEvent],
) -> str:
    """
    生成 FFmpeg filter_complex 视频滤镜。

    Args:
        main_info (VideoInfo): 主视频信息。
        events (list[InsertEvent]): 小视频插入事件。
        horizontal_events (list[HorizontalEvent]): 横屏素材叠加事件。

    Returns:
        str: FFmpeg filter_complex 参数内容。
    """
    parts = ["[0:v]setpts=PTS-STARTPTS[v0]"]

    for index, event in enumerate(events, start=1):
        start = _fmt_time(event.start_time)
        end = _fmt_time(_overlay_end_time(events, index - 1))
        pad_filter = ""
        if _should_pad_overlay_tail(events, index - 1):
            pad_filter = f"tpad=stop_mode=clone:stop_duration={_fmt_time(BOUNDARY_PAD_SECONDS)},"
        scale_crop = (
            f"scale={main_info.width}:{main_info.height}:force_original_aspect_ratio=increase,"
            f"crop={main_info.width}:{main_info.height},setsar=1,format=yuv420p,"
            f"{pad_filter}setpts=PTS-STARTPTS+{start}/TB"
        )
        parts.append(f"[{index}:v]{scale_crop}[ov{index}]")
        parts.append(
            f"[v{index - 1}][ov{index}]"
            f"overlay=0:0:enable='between(t,{start},{end})':"
            f"eof_action=pass:repeatlast=0:shortest=0[v{index}]"
        )

    base_index = len(events)
    horizontal_height = _even_dimension(main_info.height / 3)
    for offset, event in enumerate(horizontal_events, start=1):
        input_index = len(events) + offset
        output_index = base_index + offset
        start = _fmt_time(event.start_time)
        scale_bottom = (
            f"scale=-2:{horizontal_height},setsar=1,format=yuv420p,"
            f"setpts=PTS-STARTPTS+{start}/TB"
        )
        parts.append(
            f"[{input_index}:v]{scale_bottom}[hov{offset}]"
        )
        parts.append(
            f"[v{output_index - 1}][hov{offset}]"
            "overlay=(main_w-overlay_w)/2:main_h-overlay_h:"
            f"enable='between(t,{start},{_fmt_time(event.end_time)})':"
            f"eof_action=pass:repeatlast=0:shortest=0[v{output_index}]"
        )

    return ";".join(parts)


def _choose_clip_window(
    settings: OverlaySettings | None,
    fallback_duration: float,
    asset_path: Path,
    duration_by_asset: dict[Path, float] | None,
    rng: random.Random,
) -> tuple[float, float]:
    """
    为单个小视频素材随机选择截取起点和时长。

    Args:
        settings (OverlaySettings | None): 随机截取区间设置。
        fallback_duration (float): 没有 settings 时使用的固定时长。
        asset_path (Path): 当前素材路径。
        duration_by_asset (dict[Path, float] | None): 已探测的素材时长。
        rng (random.Random): 随机数生成器。

    Returns:
        tuple[float, float]: 截取起始秒和截取时长。
    """
    if settings is None:
        return 0.0, fallback_duration

    asset_duration = None
    if duration_by_asset is not None:
        asset_duration = duration_by_asset.get(asset_path)

    start_max = settings.clip_start_max
    if asset_duration is not None:
        start_max = min(start_max, max(settings.clip_start_min, asset_duration - 0.001))
    clip_start = rng.uniform(settings.clip_start_min, start_max)
    clip_end = rng.uniform(settings.clip_end_min, settings.clip_end_max)
    if asset_duration is not None and settings.short_asset_policy == "trim":
        clip_end = min(clip_end, asset_duration)
    return clip_start, clip_end - clip_start


def _overlay_end_time(events: list[InsertEvent], index: int) -> float:
    """
    计算小视频叠加层的结束时间。

    Args:
        events (list[InsertEvent]): 小视频插入事件列表。
        index (int): 当前事件下标。

    Returns:
        float: 叠加层结束时间。相邻片段会额外覆盖一小段边界时间。
    """
    event = events[index]
    if _should_pad_overlay_tail(events, index):
        return event.end_time + BOUNDARY_PAD_SECONDS
    return event.end_time


def _should_pad_overlay_tail(events: list[InsertEvent], index: int) -> bool:
    """
    判断当前小视频末尾是否需要补一小段最后帧。

    Args:
        events (list[InsertEvent]): 小视频插入事件列表。
        index (int): 当前事件下标。

    Returns:
        bool: 下一段紧接当前段时返回 True。
    """
    next_index = index + 1
    if next_index >= len(events):
        return False
    current_event = events[index]
    next_event = events[next_index]
    return next_event.start_time <= current_event.end_time + 0.000001


def _even_dimension(value: float) -> int:
    """
    把尺寸转换为 FFmpeg 更容易编码的偶数像素。

    Args:
        value (float): 原始尺寸。

    Returns:
        int: 至少为 2 的偶数尺寸。
    """
    dimension = max(2, int(round(value)))
    if dimension % 2 == 1:
        dimension -= 1
    return max(2, dimension)


def _build_horizontal_events(
    settings: OverlaySettings,
    main_duration: float,
    progress: ProgressCallback,
) -> list[HorizontalEvent]:
    """
    读取横屏素材文件夹并生成叠加计划。

    Args:
        settings (OverlaySettings): 导出设置。
        main_duration (float): 主视频时长。
        progress (ProgressCallback): 进度回调。

    Returns:
        list[HorizontalEvent]: 横屏素材叠加计划。
    """
    if settings.horizontal_asset_folder is None:
        return []

    horizontal_assets = scan_video_assets(settings.horizontal_asset_folder)
    if not horizontal_assets:
        progress("横屏素材文件夹为空，跳过横屏素材叠加。")
        return []

    progress("读取横屏素材信息...")
    duration_by_asset: dict[Path, float] = {}
    for asset_path, info in _probe_videos(horizontal_assets, settings.probe_workers):
        duration_by_asset[asset_path] = info.duration

    return build_horizontal_schedule(main_duration, horizontal_assets, duration_by_asset)


def _probe_videos(assets: list[Path], probe_workers: int) -> list[tuple[Path, VideoInfo]]:
    """
    按指定线程数探测视频信息。

    Args:
        assets (list[Path]): 需要探测的视频路径列表。
        probe_workers (int): 最大并发探测线程数。

    Returns:
        list[tuple[Path, VideoInfo]]: 按输入顺序返回的视频信息。
    """
    if not assets:
        return []

    worker_count = max(1, probe_workers)
    if worker_count == 1 or len(assets) == 1:
        return [(asset, probe_video(asset)) for asset in assets]

    result_by_asset: dict[Path, VideoInfo] = {}
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_by_asset = {executor.submit(probe_video, asset): asset for asset in assets}
        for future in as_completed(future_by_asset):
            asset = future_by_asset[future]
            result_by_asset[asset] = future.result()

    return [(asset, result_by_asset[asset]) for asset in assets]


def _run_ffmpeg(
    cmd: list[str],
    progress: ProgressCallback,
    cancel_event: threading.Event,
) -> None:
    output_queue: list[str] = []
    output_lock = threading.Lock()

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_creationflags(),
    )

    last_line = ""
    reader = threading.Thread(target=_read_process_output, args=(process, output_queue, output_lock), daemon=True)
    reader.start()

    while True:
        if cancel_event.is_set():
            _terminate_process(process)
            raise OverlayError("导出已取消。")

        with output_lock:
            lines = output_queue[:]
            output_queue.clear()
        for line in lines:
            last_line = line
            if "time=" in line or line.startswith("frame="):
                progress(line)

        exit_code = process.poll()
        if exit_code is not None:
            break
        time.sleep(0.02)

    reader.join(timeout=1)
    with output_lock:
        lines = output_queue[:]
        output_queue.clear()
    for line in lines:
        last_line = line
        if "time=" in line or line.startswith("frame="):
            progress(line)

    if exit_code != 0:
        raise OverlayError(f"FFmpeg 导出失败：{last_line or '未知错误'}")


def _read_process_output(
    process: subprocess.Popen[str],
    output_queue: list[str],
    output_lock: threading.Lock,
) -> None:
    if process.stdout is None:
        return
    for raw_line in process.stdout:
        line = raw_line.strip()
        if not line:
            continue
        with output_lock:
            output_queue.append(line)


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def _parse_duration(text: str) -> float:
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if not match:
        return 0.0
    hours = int(match.group(1))
    minutes = int(match.group(2))
    seconds = float(match.group(3))
    return hours * 3600 + minutes * 60 + seconds


def _parse_video_size(text: str) -> tuple[int, int]:
    for line in text.splitlines():
        if " Video:" not in line and "Video:" not in line:
            continue
        matches = re.findall(r"(?<![A-Za-z0-9])(\d{2,5})x(\d{2,5})(?![A-Za-z0-9])", line)
        for width_text, height_text in matches:
            width = int(width_text)
            height = int(height_text)
            if width > 0 and height > 0:
                return width, height
    return 0, 0


def _fmt_time(value: float) -> str:
    if not math.isfinite(value):
        raise OverlayError("时间参数无效。")
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _creationflags() -> int:
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)
