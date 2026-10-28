import uuid
from typing import Any, Callable, ClassVar, Optional
from core.utils.logger import logger
from core.utils.config import ConfigManager


class __GlobalStream:
    def __init__(self):
        self.readers = {}
        self.on_output_data = None

    def register_reader(self, reader):
        if reader.id not in self.readers:
            self.readers[reader.id] = reader

    def unregister_reader(self, reader) -> None:
        if reader.id in self.readers:
            del self.readers[reader.id]

    def has_readers(self) -> bool:
        """还有没有订阅者。没有就说明这次数据没人要，连处理都不必做。"""
        return bool(self.readers)

    def input(self, data: bytes) -> None:
        # 无订阅者时直接丢弃。上游 XiaoAI.on_input_data() 也会据此早退，
        # 双重保险：万一有人绕过上游，这里也不会白跑一圈。
        if not self.readers:
            return
        # 复制一份 id 列表：reader.input() 理论上可能触发注销（例如内部出错自停），
        # 边遍历边改 dict 会抛 RuntimeError。
        for key in list(self.readers.keys()):
            reader = self.readers.get(key)
            if reader is not None:
                reader.input(data)

    def output(self, frames: bytes) -> None:
        if self.on_output_data:
            self.on_output_data(frames)


GlobalStream = __GlobalStream()


class MyStream:
    def __init__(
        self,
        rate: int,
        channels: int,
        format: int,
        input: bool = False,
        output: bool = False,
        frames_per_buffer: int = 1024,
        start: bool = True,
    ) -> None:
        self.id = uuid.uuid4()
        self._rate = rate
        self._channels = channels
        self._format = format
        self._frames_per_buffer = frames_per_buffer
        self._is_input = input
        self._is_output = output
        self._is_active = False

        self.input_bytes = bytearray()
        self._read_offset = 0

        # 缓冲区回收计数（诊断用，不改逻辑）
        # overflow: 撞 _max_buf 上限、被迫丢弃最旧数据的次数（这里会 memmove）
        # trim:     读偏移超过 256KB 后批量回收的次数（同样 memmove，但频率低）
        self._overflow_hits = 0
        self._overflow_bytes = 0
        self._trim_hits = 0

        if start:
            self.start_stream()

    def close(self) -> None:
        self.stop_stream()

    def is_active(self) -> bool:
        return self._is_active

    def start_stream(self) -> None:
        if not self._is_active:
            self._is_active = True
            if self._is_input:
                GlobalStream.register_reader(self)

    def stop_stream(self) -> None:
        if self._is_active:
            self._is_active = False
            if self._is_input:
                GlobalStream.unregister_reader(self)
                self.input_bytes.clear()
                # 缓冲区已清空，读偏移必须一起归零。
                # 否则 resume 后新数据从 0 开始堆，而 read() 仍从旧的
                # _read_offset 处取，要等缓冲区重新长过旧偏移才读得到
                # —— 表现为「唤醒后说话，VAD 十几秒内听不见」。
                self._read_offset = 0

    def write(self, frames: bytes) -> None:
        # 发送输出音频流到扬声器
        if not self._is_output or not self._is_active:
            return
        GlobalStream.output(frames)

    def input(self, data: bytes):
        # 收到麦克风输入音频流
        if not self._is_input or not self._is_active:
            return

        if len(data) > 0:
            self.input_bytes.extend(data)

        # 缓冲区上限（bytes），防止积压时无限膨胀导致 CPU 持续死磕老数据
        # 默认 512KB（≈16秒 @16kHz 16bit），可在 config.py 中调整
        _max_buf = ConfigManager.instance().get_app_config("audio_input.max_input_buffer_size", 524288)
        if len(self.input_bytes) > _max_buf:
            overflow = len(self.input_bytes) - _max_buf
            self._overflow_hits += 1
            self._overflow_bytes += overflow
            # 首次 + 每 100 次打一条，避免刷屏
            if self._overflow_hits == 1 or self._overflow_hits % 100 == 0:
                logger.warning(
                    f"输入缓冲溢出 #{self._overflow_hits}：丢弃 {overflow}B"
                    f"（累计 {self._overflow_bytes}B，当前缓冲 {len(self.input_bytes)}B）"
                    f" —— 消费速度跟不上音频流入，此处会 memmove 剩余数据",
                    module="Stream",
                )
            del self.input_bytes[:overflow]
            self._read_offset = max(0, self._read_offset - overflow)

    def read(self, num_frames=None, exception_on_overflow=False) -> bytes:
        if num_frames is None:
            data = bytes(self.input_bytes)
            self.input_bytes.clear()
            self._read_offset = 0
            return data

        bytes_needed = num_frames * 2
        if (
            not self._is_input
            or not self._is_active
            # 达不到预期长度时，返回空字节，等待下一次读取
            or len(self.input_bytes) - self._read_offset < bytes_needed
        ):
            return bytes([])

        # 偏移读：只取需要的部分，不删除剩余数据
        data = bytes(self.input_bytes[self._read_offset:self._read_offset + bytes_needed])
        self._read_offset += bytes_needed

        # 延迟回收：累积偏移超过阈值才批量清理已读数据
        if self._read_offset > 262144:
            self._trim_hits += 1
            del self.input_bytes[:self._read_offset]
            self._read_offset = 0

        return data

    def buffer_stats(self) -> dict:
        """缓冲区诊断快照（只读，不影响数据）"""
        return {
            "buf_bytes": len(self.input_bytes),
            "read_offset": self._read_offset,
            "overflow_hits": self._overflow_hits,
            "overflow_bytes": self._overflow_bytes,
            "trim_hits": self._trim_hits,
        }

    def frameSize(self):
        return len(self.input_bytes)


class MyAudio:
    """PyAudio 替代品，用于创建和管理音频流"""

    Stream: ClassVar[type] = MyStream

    @classmethod
    def create(cls):
        # 使用小爱音箱音频（通过 Rust 补丁）
        return MyAudio()

    @classmethod
    def get_input_device_index(cls, audio):
        return 0

    @classmethod
    def get_output_device_index(cls, audio):
        return 0

    def __init__(self) -> None:
        self._is_terminated = False

    def open(
        self,
        rate: int,
        channels: int,
        format: int,
        input: bool = False,
        output: bool = False,
        input_device_index: Optional[int] = None,
        output_device_index: Optional[int] = None,
        frames_per_buffer: int = 1024,
        start: bool = True,
        input_host_api_specific_stream_info: Optional[Any] = None,
        output_host_api_specific_stream_info: Optional[Any] = None,
        stream_callback: Optional[Callable] = None,
    ) -> MyStream:
        if self._is_terminated:
            raise RuntimeError("MyAudio instance has been terminated")

        return MyStream(
            rate=rate,
            channels=channels,
            format=format,
            input=input,
            output=output,
            frames_per_buffer=frames_per_buffer,
            start=start,
        )

    def terminate(self) -> None:
        if not self._is_terminated:
            self._is_terminated = True
