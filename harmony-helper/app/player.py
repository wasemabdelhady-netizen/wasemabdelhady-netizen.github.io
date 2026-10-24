"""Preview playback with live volume: the song bed and the line are mixed chunk by
chunk, so moving «علو الأغنية» / «علو الخط» is heard while it plays.

Windows: streams through winmm waveOut (ctypes, no extra packages).
Elsewhere, or if no audio device opens: plays a rendered WAV and restarts it
from the same spot when a slider moves.
"""
import os
import subprocess
import sys
import tempfile
import threading
import time
import wave

import numpy as np

SR = 44100
CHUNK = 2048     # frames per buffer (~46 ms)
NBUF = 6         # buffers queued (~0.28 s latency)


class LiveMix:
    """bed: (n, 2) float, line: (n,) float, already scaled; out = (song·bed + line·[0.8, 1]) · limit."""

    def __init__(self, bed, line, limit=1.0, song_vol=1.0, line_vol=1.0, pos=0):
        self.bed, self.line, self.limit = bed, line, limit
        self.song_vol, self.line_vol = song_vol, line_vol  # set from the GUI thread at any time
        self._g = (song_vol, line_vol)
        self.pos = pos

    @property
    def done(self):
        return self.pos >= len(self.bed)

    def next_chunk(self, n=CHUNK):
        """Next n frames as 16-bit stereo bytes (None at the end). Gains ramp across the chunk: no clicks."""
        a = self.pos
        b = min(a + n, len(self.bed))
        if a >= b:
            return None
        k = b - a
        g0s, g0l = self._g
        g1s, g1l = float(self.song_vol), float(self.line_vol)
        ramp = np.linspace(0.0, 1.0, k, endpoint=False)
        gs = (g0s + (g1s - g0s) * ramp)[:, None]
        gl = g0l + (g1l - g0l) * ramp
        out = self.bed[a:b] * gs
        ln = self.line[a:b] * gl
        out[:, 0] += 0.8 * ln
        out[:, 1] += ln
        out *= self.limit
        self._g = (g1s, g1l)
        self.pos = b
        return (np.clip(out, -1, 1) * 32767).astype("<i2").tobytes()

    def render(self):
        """Whole remaining mix at the current gains (no ramp)."""
        self._g = (float(self.song_vol), float(self.line_vol))
        parts = []
        while True:
            c = self.next_chunk(1 << 16)
            if c is None:
                return b"".join(parts)
            parts.append(c)


# ----------------------------------------------------------------------------- Windows waveOut

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class WAVEFORMATEX(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD),
                    ("nSamplesPerSec", wintypes.DWORD), ("nAvgBytesPerSec", wintypes.DWORD),
                    ("nBlockAlign", wintypes.WORD), ("wBitsPerSample", wintypes.WORD), ("cbSize", wintypes.WORD)]

    class WAVEHDR(ctypes.Structure):
        _fields_ = [("lpData", ctypes.c_void_p), ("dwBufferLength", wintypes.DWORD),
                    ("dwBytesRecorded", wintypes.DWORD), ("dwUser", ctypes.c_size_t),
                    ("dwFlags", wintypes.DWORD), ("dwLoops", wintypes.DWORD),
                    ("lpNext", ctypes.c_void_p), ("reserved", ctypes.c_size_t)]

    WAVE_MAPPER = 0xFFFFFFFF
    WHDR_DONE = 0x1
    _w = ctypes.windll.winmm
    _w.waveOutOpen.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint, ctypes.POINTER(WAVEFORMATEX),
                               ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD]
    _w.waveOutOpen.restype = ctypes.c_uint
    for _f in (_w.waveOutPrepareHeader, _w.waveOutUnprepareHeader, _w.waveOutWrite):
        _f.argtypes = [ctypes.c_void_p, ctypes.POINTER(WAVEHDR), ctypes.c_uint]
        _f.restype = ctypes.c_uint
    for _f in (_w.waveOutReset, _w.waveOutClose):
        _f.argtypes = [ctypes.c_void_p]
        _f.restype = ctypes.c_uint

    def wave_out_open():
        fmt = WAVEFORMATEX(1, 2, SR, SR * 4, 4, 16, 0)  # PCM, stereo, 16-bit
        h = ctypes.c_void_p()
        err = _w.waveOutOpen(ctypes.byref(h), WAVE_MAPPER, ctypes.byref(fmt), 0, 0, 0)
        if err:
            raise OSError("waveOutOpen error %d (no audio device?)" % err)
        return h


class WaveOutStream:
    """Feeds LiveMix chunks to waveOut from a background thread."""
    name = "waveout"

    def __init__(self, mix):
        self.mix = mix
        self.h = wave_out_open()
        self._stop = False
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        size = ctypes.sizeof(WAVEHDR)
        hdrs = [WAVEHDR() for _ in range(NBUF)]
        bufs = [ctypes.create_string_buffer(CHUNK * 4) for _ in range(NBUF)]
        busy = [False] * NBUF
        try:
            while not self._stop:
                for k in range(NBUF):
                    if busy[k]:
                        if not hdrs[k].dwFlags & WHDR_DONE:
                            continue
                        _w.waveOutUnprepareHeader(self.h, ctypes.byref(hdrs[k]), size)
                        busy[k] = False
                    data = self.mix.next_chunk(CHUNK)
                    if data is None:
                        continue
                    ctypes.memmove(bufs[k], data, len(data))
                    hdrs[k] = WAVEHDR(ctypes.cast(bufs[k], ctypes.c_void_p), len(data), 0, 0, 0, 0, None, 0)
                    _w.waveOutPrepareHeader(self.h, ctypes.byref(hdrs[k]), size)
                    _w.waveOutWrite(self.h, ctypes.byref(hdrs[k]), size)
                    busy[k] = True
                if self.mix.done and not any(busy):
                    break
                time.sleep(0.01)
        finally:
            _w.waveOutReset(self.h)
            for k in range(NBUF):
                if busy[k]:
                    _w.waveOutUnprepareHeader(self.h, ctypes.byref(hdrs[k]), size)
            _w.waveOutClose(self.h)

    def update(self):
        pass  # the mixer reads the gains for every chunk

    def stop(self):
        self._stop = True
        self.t.join(1.0)


# ----------------------------------------------------------------------------- fallback: WAV file

class FileStream:
    """Plays the remaining mix as a WAV; on a volume change, re-renders from where it is now."""
    name = "file"

    def __init__(self, mix):
        self.mix, self.proc, self.path = mix, None, None
        self._start(mix.pos)

    def _start(self, pos):
        self._kill()
        self.mix.pos = pos
        self.t0, self.pos0 = time.time(), pos
        fd, self.path = tempfile.mkstemp(prefix="harmony_", suffix=".wav")
        os.close(fd)
        with open(self.path, "wb") as fh:
            w = wave.open(fh, "wb")
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(self.mix.render())
            w.close()
        if sys.platform == "win32":
            import winsound
            winsound.PlaySound(self.path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
            return
        for cmd in (["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", self.path],
                    ["paplay", self.path], ["aplay", "-q", self.path]):
            try:
                self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except OSError:
                continue

    def position(self):
        return min(len(self.mix.bed), self.pos0 + int((time.time() - self.t0) * SR))

    def update(self):
        pos = self.position()
        if pos < len(self.mix.bed):
            self._start(pos)

    def _kill(self):
        if sys.platform == "win32":
            import winsound
            winsound.PlaySound(None, 0)
        elif self.proc:
            self.proc.kill()
            self.proc = None
        if self.path:
            try:
                os.remove(self.path)
            except OSError:
                pass  # still open by the player; temp folder cleans up
            self.path = None

    def stop(self):
        self._kill()


class Player:
    def __init__(self):
        self.stream = None
        self.mix = None

    @property
    def backend(self):
        return self.stream.name if self.stream else None

    def play(self, bed, line, limit, song_vol=1.0, line_vol=1.0):
        self.stop()
        self.mix = LiveMix(bed, line, limit, song_vol, line_vol)
        if sys.platform == "win32":
            try:
                self.stream = WaveOutStream(self.mix)
                return
            except OSError:
                pass
        self.stream = FileStream(self.mix)

    def set_volume(self, song_vol, line_vol):
        """Live: waveOut picks it up on the next chunk; the WAV fallback restarts from the same spot."""
        if self.mix is None:
            return
        self.mix.song_vol, self.mix.line_vol = song_vol, line_vol
        if self.stream:
            self.stream.update()

    def stop(self):
        if self.stream:
            self.stream.stop()
        self.stream = None
