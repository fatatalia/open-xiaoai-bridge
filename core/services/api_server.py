"""
HTTP API Server for XiaoZhi
Provides endpoints to play text/audio remotely
"""

import asyncio
import json
import os
import tempfile
from collections.abc import Coroutine
from typing import Any

import open_xiaoai_server
from aiohttp import web
from core.ref import get_speaker, get_vad, get_xiaoai
from core.services.audio.asr.sherpa import SherpaASR
from core.services.audio.gate import AudioGate
from core.services.tts.doubao import DoubaoTTS
from core.services.tts.filler import FillerPlayer
from core.utils.config import ConfigManager
from core.utils.logger import logger


class APIServer:
    """HTTP API Server to control XiaoZhi speaker remotely"""

    def __init__(self, host: str = "0.0.0.0", port: int = 8080):
        self.host = host
        self.port = port
        self.config = ConfigManager.instance()
        self.app = web.Application()
        self.runner = None
        self.site = None
        self._setup_routes()

    def _setup_routes(self):
        """Setup API routes"""
        self.app.router.add_post("/api/play/text", self.handle_play_text)
        self.app.router.add_post("/api/play/url", self.handle_play_url)
        self.app.router.add_post("/api/play/file", self.handle_play_file)
        self.app.router.add_get("/api/status", self.handle_get_status)
        self.app.router.add_post("/api/wakeup", self.handle_wakeup)
        self.app.router.add_post("/api/interrupt", self.handle_stop)
        self.app.router.add_get("/api/health", self.handle_health)
        # 音频输入门控（电视开着时彻底停掉 KWS/VAD）
        self.app.router.add_post("/api/audio/gate", self.handle_audio_gate)
        self.app.router.add_get("/api/audio/gate", self.handle_audio_gate_status)
        # TTS endpoints
        self.app.router.add_post("/api/tts/doubao", self.handle_tts_doubao)
        self.app.router.add_get("/api/tts/doubao_voices", self.handle_tts_voices)
        self.app.router.add_post("/api/tts/moss", self.handle_tts_moss)
        self.app.router.add_post("/api/tts/piper", self.handle_tts_piper)
        self.app.router.add_post("/api/asr/listen", self.handle_asr_listen)

    def _create_background_task(
        self,
        coro: Coroutine[Any, Any, Any],
        name: str,
    ) -> asyncio.Task:
        """Create a background task and log any unhandled exception."""
        task = asyncio.create_task(coro)

        def _log_task_result(done_task: asyncio.Task):
            try:
                done_task.result()
            except Exception as exc:
                logger.error(f"[APIServer] Background task failed ({name}): {exc}")

        task.add_done_callback(_log_task_result)
        return task

    async def start(self):
        """Start the HTTP server"""
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.host, self.port)
        await self.site.start()
        logger.info(f"[APIServer] HTTP server started at http://{self.host}:{self.port}")

    async def stop(self):
        """Stop the HTTP server"""
        if self.runner:
            await self.runner.cleanup()
            logger.info("[APIServer] HTTP server stopped")

    # ============ Handlers ============

    async def handle_play_text(self, request: web.Request) -> web.Response:
        """
        POST /api/play/text
        Play text via TTS

        Request body:
            {
                "text": "你好",           # required
                "blocking": false,        # optional, default false
                "timeout": 60000          # optional, timeout in ms
            }
        """
        try:
            data = await request.json()
            text = data.get("text")

            if not text:
                return web.json_response(
                    {"success": False, "error": "Missing required field: text"},
                    status=400
                )

            blocking = data.get("blocking", False)
            timeout = data.get("timeout", 10 * 60 * 1000)

            speaker = get_speaker()
            if not speaker:
                return web.json_response(
                    {"success": False, "error": "Speaker not initialized"},
                    status=503
                )

            # Run in background to not block the response
            if blocking:
                result = await speaker.play(text=text, blocking=True, timeout=timeout)
                return web.json_response({"success": result})
            else:
                asyncio.create_task(speaker.play(text=text, blocking=False, timeout=timeout))
                return web.json_response({"success": True, "message": "Playing text in background"})

        except json.JSONDecodeError:
            return web.json_response(
                {"success": False, "error": "Invalid JSON"},
                status=400
            )
        except Exception as e:
            logger.error(f"[APIServer] Error playing text: {e}")
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500
            )

    async def handle_play_url(self, request: web.Request) -> web.Response:
        """
        POST /api/play/url
        Play audio from URL

        Request body:
            {
                "url": "http://example.com/audio.mp3",  # required
                "blocking": false,                       # optional, default false
                "timeout": 60000                         # optional, timeout in ms
            }
        """
        try:
            data = await request.json()
            url = data.get("url")

            if not url:
                return web.json_response(
                    {"success": False, "error": "Missing required field: url"},
                    status=400
                )

            blocking = data.get("blocking", False)
            timeout = data.get("timeout", 10 * 60 * 1000)

            speaker = get_speaker()
            if not speaker:
                return web.json_response(
                    {"success": False, "error": "Speaker not initialized"},
                    status=503
                )

            if blocking:
                result = await speaker.play(url=url, blocking=True, timeout=timeout)
                return web.json_response({"success": result})
            else:
                asyncio.create_task(speaker.play(url=url, blocking=False, timeout=timeout))
                return web.json_response({"success": True, "message": "Playing URL in background"})

        except json.JSONDecodeError:
            return web.json_response(
                {"success": False, "error": "Invalid JSON"},
                status=400
            )
        except Exception as e:
            logger.error(f"[APIServer] Error playing URL: {e}")
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500
            )

    async def handle_play_file(self, request: web.Request) -> web.Response:
        """
        POST /api/play/file
        Upload and play audio file directly via audio buffer

        Request: multipart/form-data
            - file: audio file (required, mp3/wav/opus etc.)

        Query params:
            - blocking: true/false (optional, default false)
            - sample_rate: target sample rate in Hz (optional, default 24000, can be 48000, 44100, etc.)

        Response:
            {
                "success": true,
                "message": "File played"
            }
        """
        try:
            # Parse query params
            blocking = request.query.get("blocking", "false").lower() == "true"
            sample_rate = int(request.query.get("sample_rate", "24000"))

            reader = await request.multipart()

            # Get the file field
            field = await reader.next()
            if not field or field.name != "file":
                return web.json_response(
                    {"success": False, "error": "Missing required field: file"},
                    status=400
                )

            # Check filename
            filename = field.filename
            if not filename:
                return web.json_response(
                    {"success": False, "error": "No filename provided"},
                    status=400
                )

            speaker = get_speaker()
            if not speaker:
                return web.json_response(
                    {"success": False, "error": "Speaker not initialized"},
                    status=503
                )

            suffix = os.path.splitext(filename)[1] or ".mp3"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temp_file:
                total_size = 0
                while True:
                    chunk = await field.read_chunk(size=8192)
                    if not chunk:
                        break
                    temp_file.write(chunk)
                    total_size += len(chunk)
                temp_path = temp_file.name

            logger.info(f"[APIServer] Received file: {filename}, size: {total_size} bytes, blocking={blocking}, sample_rate={sample_rate}")
            logger.info(f"[APIServer] Saved upload to temp file: {temp_path}")

            async def play_audio():
                try:
                    success = await speaker.play_server_file(
                        temp_path,
                        blocking=True,
                        sample_rate=sample_rate,
                    )
                    if success:
                        logger.info(f"[APIServer] Finished playing: {filename}")
                    else:
                        logger.error(f"[APIServer] Error playing file: {filename}")
                    return success
                finally:
                    if os.path.exists(temp_path):
                        os.unlink(temp_path)

            if blocking:
                # Wait for playback to complete
                success = await play_audio()
                return web.json_response({
                    "success": success,
                    "message": f"Finished playing: {filename}",
                    "filename": filename,
                    "size": total_size,
                    "sample_rate": sample_rate
                })
            else:
                # Run in background
                asyncio.create_task(play_audio())
                return web.json_response({
                    "success": True,
                    "message": f"Playing file: {filename}",
                    "filename": filename,
                    "size": total_size,
                    "sample_rate": sample_rate
                })

        except Exception as e:
            logger.error(f"[APIServer] Error handling file upload: {e}")
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500
            )

    async def handle_get_status(self, request: web.Request) -> web.Response:
        """
        GET /api/status
        Get current speaker status
        """
        try:
            speaker = get_speaker()

            if not speaker:
                return web.json_response(
                    {"success": False, "error": "Speaker not initialized"},
                    status=503
                )

            status = await speaker.get_playing()

            return web.json_response({
                "success": True,
                "data": {
                    "status": status  # "playing", "paused", "idle"
                }
            })

        except Exception as e:
            logger.error(f"[APIServer] Error getting status: {e}")
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500
            )

    async def handle_wakeup(self, request: web.Request) -> web.Response:
        """
        POST /api/wakeup
        Wake up the speaker

        Request body:
            {
                "silent": false   # optional, default false (audible wakeup)
            }
        """
        try:
            data = await request.json() if request.can_read_body else {}
            silent = data.get("silent", False)

            speaker = get_speaker()
            if not speaker:
                return web.json_response(
                    {"success": False, "error": "Speaker not initialized"},
                    status=503
                )

            result = await speaker.wake_up(awake=True, silent=silent)
            return web.json_response({"success": result})

        except Exception as e:
            logger.error(f"[APIServer] Error waking up: {e}")
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500
            )

    async def handle_stop(self, request: web.Request) -> web.Response:
        """
        POST /api/interrupt
        Interrupt current playback
        """
        try:
            speaker = get_speaker()
            xiaoai = get_xiaoai()
            if not speaker:
                return web.json_response(
                    {"success": False, "error": "Speaker not initialized"},
                    status=503
                )

            await speaker.stop_device_audio()
            # 停止连续对话
            if xiaoai:
                xiaoai.stop_conversation()

            return web.json_response({"success": True})

        except Exception as e:
            logger.error(f"[APIServer] Error interrupting: {e}")
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500
            )

    async def handle_health(self, request: web.Request) -> web.Response:
        """
        GET /api/health
        Health check endpoint
        """
        return web.json_response({
            "success": True,
            "data": {
                "status": "healthy",
                "speaker_ready": get_speaker() is not None
            }
        })

    async def handle_audio_gate(self, request: web.Request) -> web.Response:
        """
        POST /api/audio/gate
        开关语音输入门控。closed=true 时 KWS/VAD 彻底停摆，
        麦克风数据到 on_input_data() 就直接丢弃，不做任何处理。

        Request body:
            {
                "closed": true,          # required
                "reason": "tv_on"        # optional，仅用于日志
            }

        典型调用者：HA 自动化（电视开/关）、手动快捷指令。
        """
        try:
            data = await request.json() if request.can_read_body else {}
        except Exception:
            return web.json_response(
                {"success": False, "error": "invalid JSON body"}, status=400
            )

        if "closed" not in data:
            return web.json_response(
                {"success": False, "error": "missing required field: closed"},
                status=400,
            )

        closed = bool(data.get("closed"))
        reason = str(data.get("reason") or "")
        gate = AudioGate.instance()
        changed = gate.close(reason) if closed else gate.open(reason)

        return web.json_response({
            "success": True,
            "data": {
                "changed": changed,
                "closed": gate.closed,
                "reason": reason,
            },
        })

    async def handle_audio_gate_status(self, request: web.Request) -> web.Response:
        """GET /api/audio/gate —— 查询当前门控状态。"""
        gate = AudioGate.instance()
        return web.json_response({"success": True, "data": gate.status()})

    async def handle_tts_doubao(self, request: web.Request) -> web.Response:
        """
        POST /api/tts/doubao
        Synthesize text using Doubao (ByteDance Volcano) TTS and play it

        Request body:
            {
                "text": "你好",                    # required
                "app_id": "your_app_id",           # optional (uses config if not provided)
                "access_key": "your_access_key",   # optional (uses config if not provided)
                "resource_id": "your_resource_id", # optional (auto-detected based on speaker_id)
                "speaker_id": "zh_female_cancan_mars_bigtts",  # optional, default voice
                "speed": 1.0,                       # optional, 0.8-2.0
                "blocking": true,                   # optional, default false
                "emotion": "happy",                 # optional, emotion for multi-emotion speakers
                "context_texts": [                   # optional, only for 2.0 speakers (only first value effective)
                    "你可以说慢一点吗？",
                    "你可以用特别痛心的语气说话吗？",
                    "你能用骄傲的语气来说话吗？"
                ]
            }
        """
        speaker_id = "<unknown>"
        resource_id_for_log = "<unknown>"
        resolved_format = "<unknown>"
        blocking = False
        use_stream = False

        try:
            data = await request.json()
            text = data.get("text")

            if not text:
                return web.json_response(
                    {"success": False, "error": "Missing required field: text"},
                    status=400
                )

            # Get credentials from request or config
            tts_config = self.config.get_app_config("tts.doubao", {})

            app_id = data.get("app_id") or tts_config.get("app_id")
            access_key = data.get("access_key") or tts_config.get("access_key")
            # resource_id is now optional - will be auto-detected based on speaker
            resource_id = data.get("resource_id") or tts_config.get("resource_id")
            resource_id_for_log = resource_id or "<auto>"

            if not all([app_id, access_key]):
                return web.json_response(
                    {"success": False, "error": "Doubao TTS credentials not configured. Provide app_id and access_key in request or config.py"},
                    status=400
                )

            speaker_id = data.get("speaker_id") or data.get("speaker") or tts_config.get("default_speaker") or "zh_female_shuangkuaisisi_moon_bigtts"
            speed = float(data.get("speed", 1.0))
            blocking = data.get("blocking", False)
            context_texts = data.get("context_texts")  # Only supported for 2.0 speakers
            emotion = data.get("emotion")  # Emotion parameter for multi-emotion speakers

            speaker = get_speaker()
            if not speaker:
                return web.json_response(
                    {"success": False, "error": "Speaker not initialized"},
                    status=503
                )

            # Create TTS instance (auto-detects resource_id if not provided)
            tts = DoubaoTTS(
                app_id=app_id,
                access_key=access_key,
                resource_id=resource_id,
                speaker=speaker_id,
            )
            resolved_format = tts.resolve_audio_format(text)
            resource_id_for_log = tts.resource_id
            logger.info(
                f"[APIServer] Doubao TTS: speaker={speaker_id}, resource_id={tts.resource_id}, format={resolved_format}"
            )

            use_stream = tts_config.get("stream", False)
            if use_stream:
                async def play_tts_stream():
                    play_fn = (
                        open_xiaoai_server.tts_stream_play
                        if blocking
                        else open_xiaoai_server.tts_stream_play_background
                    )
                    await play_fn(
                        text,
                        app_id=app_id,
                        access_key=access_key,
                        resource_id=tts.resource_id,
                        speaker=speaker_id,
                        speed=speed,
                        format=resolved_format,
                        sample_rate=24000,
                        emotion=emotion,
                        context_texts=context_texts,
                    )

                if blocking:
                    await play_tts_stream()
                else:
                    await play_tts_stream()
            else:
                async def play_tts_audio():
                    play_fn = (
                        open_xiaoai_server.tts_play
                        if blocking
                        else open_xiaoai_server.tts_play_background
                    )
                    await play_fn(
                        text,
                        app_id=app_id,
                        access_key=access_key,
                        resource_id=tts.resource_id,
                        speaker=speaker_id,
                        speed=speed,
                        format=resolved_format,
                        sample_rate=24000,
                        emotion=emotion,
                        context_texts=context_texts,
                    )
                    logger.debug("[APIServer] Finished playing TTS audio")

                if blocking:
                    try:
                        await play_tts_audio()
                    except Exception as e:
                        return web.json_response(
                            {"success": False, "error": f"TTS playback failed: {str(e)}"},
                            status=500
                        )
                else:
                    await play_tts_audio()

            if blocking:
                return web.json_response({
                    "success": True,
                    "message": f"TTS played: {text[:50]}..." if len(text) > 50 else f"TTS played: {text}",
                    "speaker_id": speaker_id,
                })

            return web.json_response(
                {
                    "success": True,
                    "message": "TTS request accepted for background playback",
                    "speaker_id": speaker_id,
                    "accepted": True,
                    "blocking": False,
                },
                status=202,
            )

        except json.JSONDecodeError:
            return web.json_response(
                {"success": False, "error": "Invalid JSON"},
                status=400
            )
        except Exception as e:
            logger.error(
                f"[APIServer] Doubao TTS failed: "
                f"speaker={speaker_id}, resource_id={resource_id_for_log}, "
                f"format={resolved_format}, blocking={blocking}, stream={use_stream}, "
                f"error={type(e).__name__}: {e}"
            )
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500
            )

    async def handle_tts_voices(self, request: web.Request) -> web.Response:
        """
        GET /api/tts/doubao_voices
        Get available TTS voices for Doubao

        Query params:
            - version: "1.0", "2.0", or "all" (optional, default shows all)
        """
        try:
            tts_config = self.config.get_app_config("tts.doubao", {})
            resource_id = tts_config.get("resource_id", "")

            # Get version from query param or auto-detect from resource_id
            version = request.query.get("version", "all")

            if version == "2.0":
                voices = DoubaoTTS.VOICES_2_0
            elif version == "1.0":
                voices = DoubaoTTS.VOICES_1_0
            else:
                voices = DoubaoTTS.list_voices()
                # Add version info for all voices
                return web.json_response({
                    "success": True,
                    "data": {
                        "provider": "doubao",
                        "resource_id": resource_id,
                        "versions": {
                            "1.0": {
                                "count": len(DoubaoTTS.VOICES_1_0),
                                "description": "豆包语音合成模型1.0",
                                "voices": DoubaoTTS.VOICES_1_0
                            },
                            "2.0": {
                                "count": len(DoubaoTTS.VOICES_2_0),
                                "description": "豆包语音合成模型2.0 - 支持情感变化、指令遵循、ASMR",
                                "voices": DoubaoTTS.VOICES_2_0
                            }
                        },
                        "total_voices": len(voices)
                    }
                })

            return web.json_response({
                "success": True,
                "data": {
                    "provider": "doubao",
                    "version": version,
                    "resource_id": resource_id,
                    "voices": voices,
                    "count": len(voices)
                }
            })
        except Exception as e:
            logger.error(f"[APIServer] Error getting voices: {e}")
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500
            )

    async def handle_tts_moss(self, request: web.Request) -> web.Response:
        """
        POST /api/tts/moss
        Synthesize text using local MOSS-TTS-Nano and play it

        Request body:
            {
                "text": "你好",         # required
                "blocking": false        # optional, default false
            }
        """
        try:
            data = await request.json()
            text = data.get("text")

            if not text:
                return web.json_response(
                    {"success": False, "error": "Missing required field: text"},
                    status=400
                )

            moss_config = self.config.get_app_config("tts.moss", {})
            moss_url = moss_config.get("url", "http://localhost:18083")
            moss_speaker = data.get("voice") or data.get("speaker") or moss_config.get("speaker", "demo-1")
            blocking = data.get("blocking", False)

            speaker = get_speaker()
            if not speaker:
                return web.json_response(
                    {"success": False, "error": "Speaker not initialized"},
                    status=503
                )

            import aiohttp

            async def synthesize_and_play_moss():
                async with aiohttp.ClientSession() as session:
                    start_resp = await session.post(
                        f"{moss_url}/api/generate-stream/start",
                        data={"text": text, "demo_id": moss_speaker, "max_new_frames": "150", "realtime_streaming_decode": "True"},
                        timeout=aiohttp.ClientTimeout(total=120)
                    )
                    result = await start_resp.json()
                    stream_id = result.get("stream_id")

                    if not stream_id:
                        logger.error(f"[APIServer] MOSS TTS failed to start stream: {result}")
                        return

                    chunks = []
                    async with session.get(
                        f"{moss_url}/api/generate-stream/{stream_id}/audio"
                    ) as audio_resp:
                        async for chunk in audio_resp.content.iter_chunked(8192):
                            if chunk:
                                chunks.append(chunk)

                    if chunks:
                        full_pcm = b"".join(chunks)
                        logger.info(f"[MOSS] synthesis complete: {len(full_pcm)}B, playing...")
                        await speaker.play(buffer=full_pcm)

                    await session.get(f"{moss_url}/api/generate-stream/{stream_id}/close")

            if blocking:
                await synthesize_and_play_moss()
            else:
                asyncio.create_task(synthesize_and_play_moss())

            return web.json_response({
                "success": True,
                "message": f"MOSS TTS: {text[:50]}..." if len(text) > 50 else f"MOSS TTS: {text}",
            })

        except Exception as e:
            logger.error(f"[APIServer] MOSS TTS error: {e}")
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500
            )

    async def handle_tts_piper(self, request: web.Request) -> web.Response:
        """
        POST /api/tts/piper
        Synthesize text using Piper local TTS and play it (subprocess + raw PCM)

        Request body:
            {
                "text": "你好",         # required
                "voice": "xiao_ya",      # optional, default from config
                "blocking": false        # optional, default false
            }
        """
        try:
            data = await request.json()
            text = data.get("text")

            if not text:
                return web.json_response(
                    {"success": False, "error": "Missing required field: text"},
                    status=400
                )

            piper_config = self.config.get_app_config("tts.piper", {})
            voice = data.get("voice") or piper_config.get("voice", "xiao_ya")
            blocking = data.get("blocking", False)

            speaker = get_speaker()
            if not speaker:
                return web.json_response(
                    {"success": False, "error": "Speaker not initialized"},
                    status=503
                )

            from core.services.tts.piper import PiperTTS
            piper = PiperTTS(piper_config)

            async def synthesize_and_play():
                filler = FillerPlayer(speaker)
                try:
                    # 开始 piper 合成前启动填充语音
                    await filler.start()

                    chunks = []
                    async for chunk in piper.synthesize_stream(text, voice=voice):
                        if chunk:
                            chunks.append(chunk)
                finally:
                    # 合成完成/异常 → 停止填充语音
                    await filler.stop()

                if chunks:
                    full_pcm = b"".join(chunks)
                    logger.info(f"[Piper] synthesis complete: {len(full_pcm)}B, playing...")
                    await speaker.play(buffer=full_pcm)
                    # 等待 TTS 播完后再清残，避免 filler 残留缓冲继续播放
                    duration_s = len(full_pcm) / 44100  # 22050Hz 16bit mono
                    await asyncio.sleep(duration_s + 0.3)
                    await speaker.stop_device_audio()
                else:
                    logger.warning("[Piper] no audio chunks generated, nothing to play")

            if blocking:
                await synthesize_and_play()
            else:
                asyncio.create_task(synthesize_and_play())

            return web.json_response({
                "success": True,
                "voice": voice,
                "message": f"Piper TTS ({voice}): {text[:50]}..." if len(text) > 50 else f"Piper TTS ({voice}): {text}",
            })

        except Exception as e:
            logger.error(f"[APIServer] Piper TTS error: {e}")
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500
            )

    async def handle_asr_listen(self, request: web.Request) -> web.Response:
        """
        POST /api/asr/listen
        Enter wakeup-like state, record speech, and return recognized text.

        This replicates the listening half of OpenClawConversationController
        without sending anything to OpenClaw — just VAD → ASR → return text.

        Request body:
            {
                "timeout": 20,            # optional, max seconds to wait for speech (default 20)
                "silent": false            # optional, skip notify beep (default false)
            }

        Response:
            {
                "success": true,
                "text": "今天天气怎么样",
                "duration_ms": 3200,       # recorded audio duration in ms
                "timeout": false
            }

        On timeout (no speech detected):
            {
                "success": true,
                "text": "",
                "duration_ms": 0,
                "timeout": true
            }
        """
        try:
            data = await request.json() if request.can_read_body else {}
            timeout = int(data.get("timeout", 20))
            silent = data.get("silent", False)

            vad = get_vad()
            speaker = get_speaker()
            if not vad:
                return web.json_response(
                    {"success": False, "error": "VAD not initialized"},
                    status=503
                )

            loop = asyncio.get_running_loop()
            recording_frames: list[bytes] = []
            is_recording = False
            vad_future: asyncio.Future = loop.create_future()

            from core.wakeup_session import EventManager

            # --- Stop playback, prepare mic ---
            if speaker:
                await speaker.stop_device_audio()

            import open_xiaoai_server
            await open_xiaoai_server.stop_recording()

            if not silent and speaker and hasattr(speaker, 'play'):
                # Short notification beep
                notify_path = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    "..", "assets", "sounds", "tts_notify.mp3",
                )
                if os.path.isfile(notify_path):
                    with open(notify_path, "rb") as f:
                        mp3_data = f.read()
                    pcm = open_xiaoai_server.decode_audio(mp3_data, format="mp3", sample_rate=24000)
                    if pcm:
                        await speaker.play(buffer=pcm)
                        duration = len(pcm) / (24000 * 2)
                        await asyncio.sleep(duration)

            await open_xiaoai_server.start_recording()

            # --- VAD hooks (same pattern as OpenClawConversationController._wait_for_speech) ---
            original_on_speech = EventManager.on_speech
            original_on_silence = EventManager.on_silence

            _orig_handle_speech = vad._handle_speech_frame
            _orig_handle_silence = vad._handle_silence_frame

            def _on_speech_hook(speech_buffer: bytes):
                nonlocal is_recording
                recording_frames.append(speech_buffer)
                is_recording = True
                logger.debug(f"[ASR API] VAD speech detected, buffer size: {len(speech_buffer)}")
                vad.resume("silence")

            def _on_silence_hook():
                nonlocal is_recording
                is_recording = False
                logger.debug(f"[ASR API] VAD silence detected, total {sum(len(f) for f in recording_frames)} bytes")
                if vad_future and not vad_future.done():
                    loop.call_soon_threadsafe(
                        vad_future.set_result, b"".join(recording_frames)
                    )

            def _recording_speech_frame(frames):
                if is_recording:
                    recording_frames.append(bytes(frames))
                _orig_handle_speech(frames)

            def _recording_silence_frame(frames):
                if is_recording:
                    recording_frames.append(bytes(frames))
                _orig_handle_silence(frames)

            EventManager.on_speech = _on_speech_hook
            EventManager.on_silence = _on_silence_hook
            vad._handle_speech_frame = _recording_speech_frame
            vad._handle_silence_frame = _recording_silence_frame

            try:
                vad.resume("speech")
                speech_bytes = await asyncio.wait_for(vad_future, timeout=timeout)
            except asyncio.TimeoutError:
                logger.debug("[ASR API] Timeout, no speech detected")
                vad.pause()
                return web.json_response({
                    "success": True,
                    "text": "",
                    "duration_ms": 0,
                    "timeout": True,
                })
            finally:
                EventManager.on_speech = original_on_speech
                EventManager.on_silence = original_on_silence
                vad._handle_speech_frame = _orig_handle_speech
                vad._handle_silence_frame = _orig_handle_silence

            # --- ASR ---
            duration_ms = int(len(speech_bytes) / (16000 * 2) * 1000)
            text = SherpaASR.asr(speech_bytes, sample_rate=16000)

            logger.info(f"[ASR API] Recognized ({duration_ms}ms): {text!r}")

            return web.json_response({
                "success": True,
                "text": text,
                "duration_ms": duration_ms,
                "timeout": False,
            })

        except json.JSONDecodeError:
            return web.json_response(
                {"success": False, "error": "Invalid JSON"},
                status=400
            )
        except Exception as e:
            logger.error(f"[ASR API] Error: {type(e).__name__}: {e}")
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500
            )
