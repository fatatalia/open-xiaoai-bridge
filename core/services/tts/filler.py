"""
Filler Player — 在 TTS 合成期间播放填充语音，让用户不会觉得卡死了

用法：
    filler = FillerPlayer(speaker, filler_dir)
    await filler.start()
    ... do slow synthesis ...
    await filler.stop()   # 停止并打断当前填充语音
"""

import asyncio
import os
import random

from core.utils.config import ConfigManager
from core.utils.logger import logger


class FillerPlayer:
    """在慢速合成期间循环播放随机填充语音"""

    FILLER_DIR_DEFAULT = os.path.expanduser(
        "~/.openclaw/workspace/piper-filler-audio"
    )

    def __init__(
        self,
        speaker,  # SpeakerManager instance
        filler_dir: str | None = None,
        interval_seconds: int | None = None,
    ):
        self._speaker = speaker

        # 配置开关（config.py 的 "filler" 段）。enabled=False 时 start() 直接 no-op，
        # 调用点不用改，stop() 也天然幂等。
        cfg = ConfigManager.instance().get_app_config("filler", {}) or {}
        self._enabled = bool(cfg.get("enabled", False))
        self._filler_dir = (
            filler_dir
            or (cfg.get("dir") or "").strip()
            or self.FILLER_DIR_DEFAULT
        )
        self._filler_dir = os.path.expanduser(self._filler_dir)
        self._interval = (
            interval_seconds
            if interval_seconds is not None
            else int(cfg.get("interval_seconds", 10))
        )
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._wav_paths: list[str] = []

        if self._enabled:
            self._load_wavs()

    def _load_wavs(self) -> None:
        """扫描填充语音目录，加载 WAV 文件路径列表"""
        if not os.path.isdir(self._filler_dir):
            logger.warning(
                f"[Filler] filler dir not found: {self._filler_dir}, "
                f"filler will be a no-op"
            )
            self._wav_paths = []
            return

        paths = sorted(
            os.path.join(self._filler_dir, f)
            for f in os.listdir(self._filler_dir)
            if f.endswith(".wav")
        )
        if not paths:
            logger.warning(f"[Filler] no WAV files in {self._filler_dir}")
            self._wav_paths = []
            return

        self._wav_paths = paths
        logger.info(
            f"[Filler] loaded {len(paths)} filler audio files from "
            f"{self._filler_dir}"
        )

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """启动填充语音循环（后台协程）"""
        if not self._enabled:
            logger.debug("[Filler] disabled by config (filler.enabled=False)")
            return

        if not self._wav_paths:
            logger.info("[Filler] no filler audio loaded, skipping")
            return

        if self.running:
            logger.warning("[Filler] already running")
            return

        self._stop_event.clear()
        self._task = asyncio.create_task(self._loop())
        logger.info("[Filler] started")

    async def stop(self) -> None:
        """停止填充语音循环并打断当前播放"""
        if not self.running:
            return

        self._stop_event.set()
        # 打断音箱上正在播放的填充语音
        await self._speaker.stop_device_audio()
        self._task = None
        logger.info("[Filler] stopped")

    async def _loop(self) -> None:
        """填充语音主循环：每 interval 秒随机播一条，不会连续重复同一条"""
        speaker = self._speaker
        wavs = self._wav_paths[:]
        interval = self._interval
        last_fpath: str | None = None

        while not self._stop_event.is_set():
            # 随机选一条，但不重复上一次的
            if len(wavs) > 1 and last_fpath:
                candidates = [p for p in wavs if p != last_fpath]
                fpath = random.choice(candidates)
            else:
                fpath = random.choice(wavs)

            last_fpath = fpath
            fname = os.path.basename(fpath)
            logger.info(f"[Filler] playing: {fname}")

            try:
                # 播放填充语音（阻塞直到播完）
                await speaker.play_server_file(fpath, blocking=True)
            except Exception as e:
                logger.warning(f"[Filler] play error ({fname}): {e}")

            # 等 interval 秒，或被打断
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=interval
                )
                break
            except asyncio.TimeoutError:
                pass  # 时间到，播下一条
