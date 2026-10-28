"""
Piper local TTS engine — Python binding, model permanence.

Loads models into memory once and reuses them across calls.
No more subprocess launch per synthesis.

Usage:
    from core.services.tts.piper import PiperTTS

    tts = PiperTTS()
    async for chunk in tts.synthesize_stream("你好", voice="huayan"):
        speaker.play(buffer=chunk)
"""

import asyncio
import os
from typing import AsyncIterator

import numpy as np
from piper import PiperVoice, SynthesisConfig

from core.utils.logger import logger

# Default models directory
DEFAULT_MODELS_DIR = os.path.expanduser("~/project/piper-models/zh/zh_CN")

# Voice → (model_rel, config_rel, sample_rate)
BUILTIN_VOICES = {
    "huayan": {
        "model": "huayan/medium/zh_CN-huayan-medium.onnx",
        "config": "huayan/medium/zh_CN-huayan-medium.onnx.json",
        "sample_rate": 22050,
    },
    "huayan_xlow": {
        "model": "huayan/x_low/zh_CN-huayan-x_low.onnx",
        "config": "huayan/x_low/zh_CN-huayan-x_low.onnx.json",
        "sample_rate": 22050,
    },
    "chaowen": {
        "model": "chaowen/medium/zh_CN-chaowen-medium.onnx",
        "config": "chaowen/medium/zh_CN-chaowen-medium.onnx.json",
        "sample_rate": 22050,
    },
    "xiao_ya": {
        "model": "xiao_ya/medium/zh_CN-xiao_ya-medium.onnx",
        "config": "xiao_ya/medium/zh_CN-xiao_ya-medium.onnx.json",
        "sample_rate": 22050,
    },
}


class PiperTTS:
    """Piper TTS — Python binding, model permanence.

    Models are cached at class level so they survive across instances.
    Preload at startup via ``await PiperTTS.preload()`` to avoid cold-start latency.
    """

    # Class-level cache: voice name → PiperVoice (shared across all instances)
    _voice_cache: dict[str, PiperVoice] = {}
    _config_cache: dict | None = None  # stash config for preload

    def __init__(self, config: dict | None = None):
        self._config = config or {}
        self._models_dir = self._resolve_models_dir()
        self._default_voice = self._config.get("voice", "xiao_ya")
        self._custom_voices = self._config.get("models", {})
        # Stash first config for preload() to use
        if PiperTTS._config_cache is None:
            PiperTTS._config_cache = self._config

    def _resolve_models_dir(self) -> str:
        md = self._config.get("models_dir", DEFAULT_MODELS_DIR)
        return os.path.expanduser(md)

    def _voice_paths(self, voice: str) -> tuple[str, str, int]:
        """Resolve (model_path, config_path, sample_rate) for a voice name."""
        if voice in self._custom_voices:
            v = self._custom_voices[voice]
            model = v.get("model", "")
            config_path = v.get("config", "")
            sr = v.get("sample_rate", 22050)
            if not os.path.isabs(model):
                model = os.path.join(self._models_dir, model)
            if not os.path.isabs(config_path):
                config_path = os.path.join(self._models_dir, config_path)
            model = os.path.expanduser(model)
            config_path = os.path.expanduser(config_path)
            return model, config_path, sr

        if voice in BUILTIN_VOICES:
            v = BUILTIN_VOICES[voice]
            model = os.path.join(self._models_dir, v["model"])
            config_path = os.path.join(self._models_dir, v["config"])
            sr = v["sample_rate"]
            return model, config_path, sr

        logger.warning(f"[PiperTTS] Unknown voice '{voice}', falling back to '{self._default_voice}'")
        return self._voice_paths(self._default_voice)

    def _load_voice(self, voice: str) -> PiperVoice:
        """Lazy-load and cache PiperVoice (model permanence, class-level)."""
        if voice in PiperTTS._voice_cache:
            return PiperTTS._voice_cache[voice]

        model_path, config_path, _ = self._voice_paths(voice)

        if not os.path.isfile(model_path):
            raise FileNotFoundError(f"Piper model not found: {model_path}")
        if not os.path.isfile(config_path):
            raise FileNotFoundError(f"Piper config not found: {config_path}")

        logger.info(f"[PiperTTS] loading voice '{voice}' from {model_path}")
        pv = PiperVoice.load(model_path, config_path)
        PiperTTS._voice_cache[voice] = pv
        logger.info(f"[PiperTTS] voice '{voice}' loaded (sample_rate={pv.config.sample_rate})")
        return pv

    @classmethod
    async def preload(cls, voice: str | None = None) -> bool:
        """Preload a voice into class-level cache.
        Call at startup to avoid cold-start latency on first TTS.
        Uses the config from the first instantiated PiperTTS, or defaults.

        Args:
            voice: Voice name to preload. Defaults to configured default.

        Returns:
            True if loaded successfully, False on error.
        """
        cfg = cls._config_cache or {}
        voice_name = voice or cfg.get("voice", "xiao_ya")
        try:
            # Create a throwaway instance just for _load_voice
            inst = cls.__new__(cls)
            inst._config = cfg
            inst._models_dir = inst._resolve_models_dir()
            inst._default_voice = voice_name
            inst._custom_voices = cfg.get("models", {})

            logger.info(f"[PiperTTS] preloading voice '{voice_name}'...")
            pv = await asyncio.to_thread(inst._load_voice, voice_name)
            logger.info(f"[PiperTTS] preload done: '{voice_name}' ({pv.config.sample_rate}Hz)")
            return True
        except Exception as e:
            logger.error(f"[PiperTTS] preload failed for '{voice_name}': {e}")
            return False

    async def synthesize_stream(self, text: str, voice: str | None = None) -> AsyncIterator[bytes]:
        """
        Synthesize text via Piper Python binding.
        Yields raw PCM chunks (16-bit signed, mono, voice sample rate).

        Args:
            text: Chinese text to synthesize.
            voice: Voice name (xiao_ya, huayan, etc.).

        Yields:
            bytes: Raw PCM chunks.
        """
        voice_name = voice or self._default_voice

        model_path, config_path, sample_rate = self._voice_paths(voice_name)
        logger.info(
            f"[PiperTTS] voice={voice_name} text='{text[:60]}...' "
            f"model={model_path}"
        )

        # Load model (cached after first call)
        pv = await asyncio.to_thread(self._load_voice, voice_name)
        syn_config = SynthesisConfig()

        total = 0
        try:
            # Run synthesize in thread to avoid blocking the event loop
            # (filler playback runs as a background task and needs the loop)
            chunks = await asyncio.to_thread(
                lambda: list(pv.synthesize(text, syn_config=syn_config))
            )
            for chunk in chunks:
                # Try pre-converted int16 PCM first, else convert from float32
                pcm = chunk._audio_int16_bytes
                if not pcm:
                    pcm = (chunk.audio_float_array * 32767)\
                        .astype(np.int16).tobytes()
                if pcm:
                    yield pcm
                    total += len(pcm)

                # Yield control so the event loop stays responsive
                await asyncio.sleep(0)
        except Exception as e:
            logger.error(f"[PiperTTS] synthesis error after {total}B: {e}")
        finally:
            logger.debug(f"[PiperTTS] done: voice={voice_name} total={total}B")

    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        chunks = []
        async for chunk in self.synthesize_stream(text, voice=voice):
            chunks.append(chunk)
        return b"".join(chunks)

    @property
    def default_sample_rate(self) -> int:
        _, _, sr = self._voice_paths(self._default_voice)
        return sr
