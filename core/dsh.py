"""dsh integration manager for xiaoai.

dsh（DeepSeek Harness）对接适配器：用 HTTP webhook 同步模式替代 OpenClaw 的
WebSocket gateway 通道。小爱音箱识别文字 → POST 到 dsh webhook（sync: true）
→ dsh 侧 agent 处理并返回回复文本 → 本管理器拿到文本走原 TTS 播放管线。

Configuration:

    1. config.py:
        APP_CONFIG = {
            "dsh": {
                "enabled": True,
                "url": "http://127.0.0.1:3080/webhook/xiaoai",
                "token": "your_webhook_bearer_token",
                "response_timeout": 90,  # Seconds to wait for agent reply
            }
        }

    2. Environment variables:
        export DSH_ENABLE=1

Usage:
    from core.dsh import DshManager
    await DshManager.send("你好")                       # send only，返回 reply 文本
    await DshManager.send_and_play_reply("你好")        # send + TTS 播放

接口签名与 core.openclaw.OpenClawManager 对齐（send / send_and_play_reply），
openclaw_conversation.py 的对话循环可无缝切换后端。
"""

import asyncio
import json
import os
import time

import aiohttp

from core.utils.base import get_env
from core.utils.config import ConfigManager
from core.utils.logger import logger


class DshManager:
    """Manager for dsh webhook (sync mode) messaging."""

    _instance = None
    _initialized = False

    # Connection config
    _enabled = False
    _url = None
    _token = None
    _response_timeout = 90  # seconds to wait for agent reply

    # TTS
    XIAOAI_TTS_SPEAKER = "xiaoai"  # Special value: use XiaoAI native TTS
    _tts_speaker = None

    # Session (informational: dsh 侧会话由 webhook hook 配置决定，这里仅记录)
    _session_key = "xiaoai-speaker"

    # Health
    last_error: str | None = None

    _spki_ed25519_prefix = None  # 不需要设备身份（HTTP Bearer 认证）

    @classmethod
    def initialize_from_config(cls, enabled: bool | None = None):
        """Initialize from config."""
        cls.reload_from_config(enabled=enabled)
        cls._initialized = True

    @classmethod
    def reload_from_config(cls, enabled: bool | None = None):
        """Reload dsh config from the config center."""
        config_manager = ConfigManager.instance()
        cfg = config_manager.get_app_config("dsh", {}) or {}
        env_enabled = get_env("DSH_ENABLE") or get_env("DSH_ENABLED")
        if enabled is None:
            cls._enabled = str(env_enabled).lower() in ("1", "true", "yes") if env_enabled else bool(cfg.get("enabled", False))
        else:
            cls._enabled = enabled
        cls._url = get_env("DSH_URL") or cfg.get("url", "http://127.0.0.1:3080/webhook/xiaoai")
        cls._token = get_env("DSH_WEBHOOK_TOKEN") or cfg.get("token", "")
        cls._response_timeout = int(cfg.get("response_timeout", 90))
        cls._tts_speaker = cfg.get("tts_speaker", "xiaoai")
        logger.info(
            f"[Dsh] reload config enabled={cls._enabled} url={cls._url} "
            f"response_timeout={cls._response_timeout}s tts_speaker={cls._tts_speaker}"
        )

    @classmethod
    def _headers(cls) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if cls._token:
            headers["Authorization"] = f"Bearer {cls._token}"
        return headers

    @classmethod
    async def send(cls, text: str, wait_response: bool = False) -> str | None:
        """Send a message to dsh webhook (sync mode).

        Args:
            text: Message text (语音识别结果).
            wait_response: If True, block until the agent's full reply arrives
                           and return reply text. If False, still returns the
                           reply (dsh sync mode is inherently synchronous), so
                           wait_response is effectively always True.

        Returns:
            reply text (str) on success, None on failure/timeout.
        """
        if not cls._initialized:
            cls.initialize_from_config()
        if not cls._enabled:
            logger.warning("[Dsh] send called but dsh is disabled")
            return None

        logger.user_speech(text, module=f"Dsh({cls._session_key})")

        timeout = aiohttp.ClientTimeout(total=cls._response_timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    cls._url,
                    headers=cls._headers(),
                    json={"message": text},
                ) as resp:
                    body = await resp.text()
                    if resp.status != 200:
                        cls.last_error = f"HTTP {resp.status}: {body[:200]}"
                        logger.error(f"[Dsh] send failed: {cls.last_error}")
                        return None
                    try:
                        data = json.loads(body)
                    except json.JSONDecodeError:
                        cls.last_error = f"bad JSON response: {body[:200]}"
                        logger.error(f"[Dsh] {cls.last_error}")
                        return None
                    if not data.get("ok"):
                        cls.last_error = data.get("error", "unknown error")
                        logger.error(f"[Dsh] response not ok: {cls.last_error}")
                        return None
                    reply = data.get("reply")
                    if not reply:
                        cls.last_error = "empty reply"
                        logger.warning(f"[Dsh] {cls.last_error}")
                        return None
                    return str(reply)
        except asyncio.TimeoutError:
            cls.last_error = f"timeout after {cls._response_timeout}s"
            logger.error(f"[Dsh] {cls.last_error}")
            return None
        except Exception as e:
            cls.last_error = f"{type(e).__name__}: {e}"
            logger.error(f"[Dsh] send error: {cls.last_error}")
            return None

    @classmethod
    async def send_and_play_reply(cls, text: str, wait_response: bool = False) -> str | None:
        """Send a message to dsh, then play the reply via TTS.

        Returns:
            The reply text if wait_response=True and playback completes,
            otherwise the reply text immediately (playback in background).
        """
        reply = await cls.send(text)
        if reply is None:
            return None

        if not wait_response:
            asyncio.create_task(cls._play_response_with_tts(reply))
            return reply

        try:
            await cls._play_response_with_tts(reply)
            return reply
        except Exception as e:
            logger.error(f"[Dsh] TTS playback error: {e}")
            return reply

    @classmethod
    async def _play_response_with_tts(
        cls,
        text: str,
        tts_speaker: str | None = None,
        playback_token: int | None = None,
    ):
        """Synthesize text and play through speaker（本机能力，独立于 OpenClaw）。

        与 OpenClaw 后端一致的听感：小爱原生 TTS（xiaoai）或豆包 TTS，经 speaker 播放。
        """
        from core.ref import get_speaker
        import open_xiaoai_server

        resolved = tts_speaker or cls._tts_speaker

        # 播放会话 token：优先用调用方传入的（对齐 openclaw_conversation 的 _play_tts）
        token = playback_token if playback_token is not None else open_xiaoai_server.begin_playback_session()

        try:
            if resolved == cls.XIAOAI_TTS_SPEAKER:
                speaker = get_speaker()
                if speaker:
                    logger.info(f"[Dsh] Playing via XiaoAI native TTS: {text[:60]}...")
                    await speaker.play(text=text, blocking=True)
                return
            # Doubao TTS 路径
            from core.services.tts.doubao import DoubaoTTS

            tts = DoubaoTTS.instance()
            speaker = get_speaker()
            if not speaker:
                logger.warning("[Dsh] No speaker available for TTS playback")
                return
            logger.info(f"[Dsh] Playing via Doubao TTS (speaker={resolved}): {text[:60]}...")
            audio = tts.synthesize(text, speaker=resolved)
            await speaker.play(buffer=audio, sample_rate=24000, token=token)
        finally:
            if playback_token is None:
                open_xiaoai_server.finish_playback_session(token)

    @classmethod
    def get_tts_speaker_for_session_key(cls, session_key: str | None = None) -> str | None:
        """Compatibility with OpenClawManager's TTS speaker lookup."""
        return cls._tts_speaker

    # ── OpenClawManager 兼容层（对话循环的"分两步"调用模式） ──────────────────
    # openclaw_conversation.py 的 local-ASR 路径用 _send_and_track + _wait_response
    # 两步调用（发送后播填充音效，再取回复）。dsh 是同步 HTTP 一次返回，
    # 因此 _send_and_track 直接完成请求并缓存回复，_wait_response 取缓存。

    _rule_prompt = ""  # 兼容：dsh 侧 prompt 由 webhook hook 的 template 决定，这里置空

    _pending_replies: dict[str, str] = {}  # fake run_id → reply text

    @classmethod
    async def _send_and_track(cls, text: str) -> str | None:
        """Send and immediately store the reply (sync HTTP), return a fake run_id."""
        reply = await cls.send(text)
        if reply is None:
            return None
        fake_id = f"dsh-{int(time.time() * 1000)}"
        cls._pending_replies[fake_id] = reply
        return fake_id

    @classmethod
    async def _wait_response(cls, run_id: str | None, timeout: float | None = None) -> str | None:
        """Return the cached reply for a fake run_id."""
        if not run_id:
            return None
        return cls._pending_replies.pop(run_id, None)
