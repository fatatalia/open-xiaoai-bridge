import asyncio
import os
import threading
import time

from core.ref import get_app, get_xiaoai, get_xiaozhi, set_kws
from core.services.audio.kws.sherpa import SherpaOnnx
from core.services.audio.stream import MyAudio
from core.services.audio.vad.silero import Silero
from core.services.protocols.typing import AudioConfig, DeviceState
from core.utils.config import ConfigManager
from core.utils.logger import logger
from core.wakeup_session import EventManager


class _KWS:
    def __init__(self):
        set_kws(self)
        self.config_manager = ConfigManager.instance()
        self.vad_threshold = 0.10
        
        # VAD 状态变量
        self.vad_active = False
        self.vad_speech_frames = 0  # 语音帧计数
        self.vad_silence_frames = 0  # 静音帧计数
        self.vad_min_silence_frames = 15  # 静默超过该帧数则判定为说完
        self.vad_start_time = time.time()
        
        # 设置帧大小为 512 以兼容 Silero VAD
        self.frame_size = 512
        self.sample_rate = 16000
        self.frame_duration_ms = (self.frame_size * 1000) / self.sample_rate

        # 性能采样（诊断用，不改判定逻辑）
        # 目的：确认 VAD / KWS 单帧耗时是否超过 frame_duration_ms(32ms) 的实时预算
        self._perf_vad = []
        self._perf_kws = []
        self._perf_window = 100  # 32ms per frame

        self.apply_runtime_config()
        self.config_manager.add_reload_listener(self._on_config_reload)

        # 门控闸门。set = 放行，clear = 阻塞。
        # 用 Event 而不是 `paused` 标志位 + sleep 轮询：清除后检测线程阻塞在
        # wait() 上，由内核挂起，真正 0 CPU；而轮询写法每秒要醒 100 次、每次都
        # 从缓冲区空读一遍。看电视时语音根本不该工作，没必要付这份钱。
        self._gate_block = threading.Event()
        self._gate_block.set()
        self._suspended = False

    def apply_runtime_config(self):
        """同步最新 KWS 相关配置。"""
        vad_config = self.config_manager.get_app_config("vad", {})
        self.vad_threshold = vad_config.get("threshold", 0.10)
        kws_config = self.config_manager.get_app_config("kws", {})
        min_silence_ms = kws_config.get("min_silence_duration", 480)
        self.vad_min_silence_frames = int(min_silence_ms / self.frame_duration_ms)

    def _on_config_reload(self, *_args):
        """配置重载后刷新运行时参数。"""
        self.apply_runtime_config()

    def start(self):
        self.audio = MyAudio.create()
        self.stream = self.audio.open(
            format=AudioConfig.FORMAT,
            channels=1,
            rate=16000,
            input=True,
            frames_per_buffer=AudioConfig.FRAME_SIZE,
            start=True,
        )

        # 启动 KWS 服务
        self.paused = False
        self.thread = threading.Thread(target=self._detection_loop, daemon=True)
        self.thread.start()
        config = ConfigManager.instance()
        keywords_score = config.get_app_config("kws.keywords_score", 2.0)
        keywords_threshold = config.get_app_config("kws.keywords_threshold", 0.2)
        min_silence_ms = config.get_app_config("kws.min_silence_duration", 480)
        logger.kws_event("关键词唤醒服务启动", f"关键词:[score:{keywords_score}, threshold:{keywords_threshold}], VAD:[threshold:{self.vad_threshold}, min_silence:{min_silence_ms}ms]")

    def get_file_path(self, file_name: str):
        current_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(current_dir, "../../../models", file_name)

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def suspend(self, reason: str = ""):
        """门控关闭：彻底停止关键词唤醒，连音频订阅都退掉。

        与 pause() 的区别：
        - pause() 只设标志位，检测线程仍每秒空转 100 次，缓冲区还会继续被写入
        - suspend() 注销 reader（数据不再进这个 stream）+ 阻塞检测线程（0 CPU）
        """
        if getattr(self, "_suspended", False):
            return
        self._suspended = True
        self.paused = True
        self._gate_block.clear()          # 先闸住线程，再断数据源
        stream = getattr(self, "stream", None)
        if stream is not None:
            try:
                stream.stop_stream()      # unregister_reader + 清缓冲 + 归零读偏移
            except Exception as exc:
                logger.warning(f"[KWS] suspend 时停止音频流失败: {exc}")
        logger.kws_event(
            "关键词唤醒已暂停", reason or "音频门控关闭（不再接收/处理麦克风数据）"
        )

    def unsuspend(self, reason: str = ""):
        """门控打开：恢复关键词唤醒。"""
        if not getattr(self, "_suspended", False):
            return
        self._suspended = False
        stream = getattr(self, "stream", None)
        if stream is not None:
            try:
                stream.start_stream()     # 重新注册为 reader
            except Exception as exc:
                logger.warning(f"[KWS] unsuspend 时恢复音频流失败: {exc}")
        self.paused = False
        self._gate_block.set()            # 放行检测线程
        logger.kws_event("关键词唤醒已恢复", reason or "音频门控打开")

    def _flush_perf(self):
        """每 100 帧输出一次 VAD / KWS 单帧耗时分布（诊断用）。"""
        def _pct(vals, p):
            if not vals:
                return 0.0
            s = sorted(vals)
            idx = min(int(len(s) * p / 100), len(s) - 1)
            return s[idx]

        vad = self._perf_vad
        kws = self._perf_kws
        budget = self.frame_duration_ms
        logger.info(
            f"[性能] 最近 {len(vad)} 帧 | "
            f"VAD p50={_pct(vad, 50):.1f} p95={_pct(vad, 95):.1f} max={max(vad):.1f}ms | "
            f"KWS n={len(kws)} "
            + (
                f"p50={_pct(kws, 50):.1f} p95={_pct(kws, 95):.1f} max={max(kws):.1f}ms"
                if kws else "未触发"
            )
            + f" | 实时预算 {budget:.0f}ms",
            module="KWS",
        )
        self._perf_vad.clear()
        self._perf_kws.clear()

    def _detection_loop(self):
        SherpaOnnx.start()
        self.stream.start_stream()
        while True:
            # 门控闸门：暂停期间阻塞在此，由内核挂起，0 CPU、0 唤醒。
            # 恢复靠 unsuspend() 里的 set() 唤醒，所以不需要超时轮询。
            self._gate_block.wait()

            # 读取缓冲区音频数据
            frames = self.stream.read(self.frame_size)
            if len(frames) != self.frame_size * 2:
                time.sleep(0.01)
                continue

            # 在说话和监听状态时，暂停 KWS
            xiaozhi = get_xiaozhi()
            if (
                not frames
                or self.paused
                or (
                    xiaozhi and xiaozhi.device_state
                    in [DeviceState.LISTENING, DeviceState.SPEAKING]
                )
            ):
                time.sleep(0.01)
                continue

            # 先进行 VAD 检测
            _t_vad = time.perf_counter()
            speech_prob = Silero.vad(frames, self.sample_rate) or 0
            self._perf_vad.append((time.perf_counter() - _t_vad) * 1000)
            if len(self._perf_vad) >= self._perf_window:
                self._flush_perf()
            is_speech = speech_prob >= self.vad_threshold
            
            if is_speech:
                # 检测到语音，立即激活
                if not self.vad_active:
                    self.vad_active = True
                    self.vad_start_time = time.time()
                    logger.debug("检测到语音，开始 KWS 检测", module="KWS")
                
                self.vad_silence_frames = 0
                
                # 只在有语音时才进行 KWS 检测
                _t_kws = time.perf_counter()
                result = SherpaOnnx.kws(frames)
                self._perf_kws.append((time.perf_counter() - _t_kws) * 1000)
                if result:
                    logger.wakeup(result, module="KWS")
                    self.on_message(result)
                    # 唤醒后重置状态
                    self.vad_active = False
                    self.vad_silence_frames = 0
                    
            else:
                # 静音处理
                if self.vad_active:
                    self.vad_silence_frames += 1
                    
                    # 在激活状态下，允许一定的静音间隙
                    if self.vad_silence_frames <= self.vad_min_silence_frames:
                        # 继续将音频送入 KWS，允许短暂的静音
                        _t_kws = time.perf_counter()
                        result = SherpaOnnx.kws(frames)
                        self._perf_kws.append((time.perf_counter() - _t_kws) * 1000)
                        if result:
                            logger.wakeup(result, module="KWS")
                            self.on_message(result)
                            self.vad_active = False
                            self.vad_silence_frames = 0
                    else:
                        # 静音超过阈值，停止处理
                        duration_ms = self.vad_silence_frames * self.frame_duration_ms
                        active_duration_ms = (time.time() - self.vad_start_time) * 1000 if hasattr(self, 'vad_start_time') else -1
                        logger.debug(
                            (
                                f"检测到持续静音（{duration_ms:.0f}ms），暂停 KWS，"
                                f"本次 KWS 监听时长 {active_duration_ms:.0f}ms"
                            ),
                            module="KWS",
                        )
                        self.vad_active = False
                        self.vad_silence_frames = 0
                        # Reset Sherpa stream to discard partial recognition state,
                        # preventing leftover audio from the previous utterance
                        # from combining with the next one.
                        SherpaOnnx.reset()

    def on_message(self, text: str):
        loop = get_app().loop if get_app() else get_xiaoai().async_loop
        logger.debug(f"[KWS] Dispatch wakeup event: {text}")
        future = asyncio.run_coroutine_threadsafe(
            EventManager.wakeup(text, "kws"),
            loop,
        )

        def _log_result(done_future):
            try:
                done_future.result()
            except Exception as exc:
                logger.error(f"[KWS] Wakeup dispatch failed: {type(exc).__name__}: {exc}")

        future.add_done_callback(_log_result)


KWS = _KWS()
