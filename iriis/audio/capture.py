"""Microphone capture on PortAudio's own thread, delivered to asyncio as float32 blocks."""
from __future__ import annotations

import asyncio

import numpy as np

from iriis.audio.devices import resolve_device


class MicStream:
    def __init__(self, device: int | str | None = None, sample_rate: int = 16000, block_ms: int = 80, aec=None):
        self.aec = aec   # iriis.audio.aec.EchoCanceller: removes her own voice from the mic
        self.device = resolve_device(device, "input")
        self.sample_rate = sample_rate
        self.blocksize = int(sample_rate * block_ms / 1000)
        self.queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=200)
        self._stream = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.overflows = 0

    def _callback(self, indata, frames, time_info, status) -> None:  # PortAudio thread
        if status and status.input_overflow:
            self.overflows += 1
        block = indata[:, 0].astype(np.float32, copy=True)
        if self.aec is not None:
            try:
                block = self.aec.clean(block)
            except Exception:  # noqa: BLE001  (never lose the mic over echo cancellation)
                pass
        assert self._loop is not None
        self._loop.call_soon_threadsafe(self._put, block)

    def _put(self, block: np.ndarray) -> None:
        if self.queue.full():
            self.queue.get_nowait()  # drop oldest rather than block the audio thread
        self.queue.put_nowait(block)

    async def start(self) -> None:
        import sounddevice as sd

        self._loop = asyncio.get_running_loop()
        self._stream = sd.InputStream(device=self.device, samplerate=self.sample_rate, channels=1, dtype="float32",
                                      blocksize=self.blocksize, callback=self._callback)
        self._stream.start()

    async def read(self) -> np.ndarray:
        return await self.queue.get()

    def drain(self) -> None:
        while not self.queue.empty():
            self.queue.get_nowait()

    async def stop(self) -> None:
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
