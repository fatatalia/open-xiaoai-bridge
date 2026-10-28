"""
AudioGate —— 音频输入总闸

用途：看电视时把整条语音输入链路彻底关掉。

"关掉"包含三层，缺一层就等于没关：
  1. 订阅层 —— KWS / VAD 两个 MyStream 都 unregister_reader，
     于是 GlobalStream.input() 无人可广播
  2. 计算层 —— XiaoAI.on_input_data() 见 GlobalStream 无读者就早退，
     连 numpy 增益处理都不做（那才是真正的大头）
  3. 线程层 —— KWS / VAD 的检测线程阻塞在 Event 上，由内核挂起，0 CPU

设计取舍：
  - 不复用 KWS.pause()/VAD.pause()。那两个只是"跳过检测"，数据照样往缓冲区堆，
    堆满后每 32ms 还要 memmove 近 1MB —— 那才是用户抱怨的"空跑吃资源"。
  - 状态只在这一处维护。KWS 和 VAD 不再各自定义"我是不是被门控了"，
    避免两份状态不同步（历史教训：条件判断分散在多处，必有一处漏改）。
"""

import threading

from core.ref import get_kws, get_vad
from core.utils.logger import logger


class AudioGate:
    """音频输入门控单例。closed=True 表示语音输入整体停摆。"""

    _instance = None
    _instance_lock = threading.Lock()

    @classmethod
    def instance(cls) -> "AudioGate":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self):
        self._lock = threading.Lock()
        self._closed = False
        self._reason = ""

    # ---------- 状态 ----------

    @property
    def closed(self) -> bool:
        return self._closed

    def status(self) -> dict:
        return {"closed": self._closed, "reason": self._reason}

    # ---------- 动作 ----------

    def _set_input_callback(self, enable: bool) -> None:
        """在 Rust 侧注册/注销 on_input_data 回调。

        注销后 Rust 收到麦克风数据时根本不会调用 Python —— 连"进函数看一眼
        没有读者就 return"的那点开销都没有。失败不影响正确性：即使注销失败，
        XiaoAI.on_input_data() 里的 has_readers() 早退仍会挡住后续处理，
        只是多一次空函数调用。
        """
        try:
            import open_xiaoai_server

            from core.xiaoai import XiaoAI

            if enable:
                open_xiaoai_server.register_fn("on_input_data", XiaoAI.on_input_data)
            else:
                open_xiaoai_server.unregister_fn("on_input_data")
        except Exception as exc:  # 降级：交给 has_readers() 早退兜底
            logger.debug(f"[AudioGate] Rust 侧回调{'注册' if enable else '注销'}失败: {exc}")

    def close(self, reason: str = "") -> bool:
        """关闭语音输入。幂等：已关闭时返回 False 且不重复操作。"""
        with self._lock:
            if self._closed:
                return False
            self._closed = True
            self._reason = reason

        kws = get_kws()
        vad = get_vad()
        errors = []
        if kws is not None:
            try:
                kws.suspend(reason)
            except Exception as exc:
                errors.append(f"KWS: {exc}")
        if vad is not None:
            try:
                vad.suspend(reason)
            except Exception as exc:
                errors.append(f"VAD: {exc}")

        # 最后才断 Rust 回调：先让 reader 退订，再断数据源，顺序反了会有一段
        # 窗口期数据进来但没人接。
        self._set_input_callback(False)

        logger.info(
            f"[AudioGate] 语音输入已关闭"
            f"{'（' + reason + '）' if reason else ''}"
            + (f"，部分组件失败: {'; '.join(errors)}" if errors else "")
        )
        return True

    def open(self, reason: str = "") -> bool:
        """恢复语音输入。幂等：已开启时返回 False。"""
        with self._lock:
            if not self._closed:
                return False
            self._closed = False
            self._reason = ""

        # 先接上 Rust 回调，再恢复 reader —— 顺序与 close() 相反，同理。
        self._set_input_callback(True)

        kws = get_kws()
        vad = get_vad()
        errors = []
        if kws is not None:
            try:
                kws.unsuspend(reason)
            except Exception as exc:
                errors.append(f"KWS: {exc}")
        if vad is not None:
            try:
                vad.unsuspend(reason)
            except Exception as exc:
                errors.append(f"VAD: {exc}")

        logger.info(
            f"[AudioGate] 语音输入已恢复"
            f"{'（' + reason + '）' if reason else ''}"
            + (f"，部分组件失败: {'; '.join(errors)}" if errors else "")
        )
        return True


AudioGate = AudioGate
