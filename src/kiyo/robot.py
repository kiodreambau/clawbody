"""Reachy Mini USB audio with local VAD, Whisper and macOS speech synthesis."""

import fcntl
import math
import os
import queue
import re
import subprocess
import tempfile
import time
import wave
from collections import deque
from contextlib import contextmanager
from pathlib import Path

from .settings import KiyoError

RATE = 16000
FRAME = 512
WAKE = re.compile(r"^(?:(?:hey|herr|hallo)[ ,]+)?(?:kiyo|kio|kijo|kyo|kiko)\b[\s,.!?;:–-]*", re.IGNORECASE)
SLEEP = {"schlaf", "schlaf weiter", "hör auf zuzuhören", "bis später", "gute nacht"}


def audio_device(settings, sd, direction):
    name = settings.get("reachy", "audio_device", "Reachy Mini Audio").casefold()
    channel_key = f"max_{direction}_channels"
    matches = [
        (index, device)
        for index, device in enumerate(sd.query_devices())
        if name in device["name"].casefold() and device[channel_key]
    ]
    if len(matches) != 1:
        raise KiyoError(f"Kiyo findet kein eindeutiges Reachy-Mini-Audiogerät ({direction}).")
    return matches[0]


def speak(settings, text):
    import numpy as np
    import sounddevice as sd
    from scipy.signal import resample_poly

    index, device = audio_device(settings, sd, "output")
    output_rate = int(device["default_samplerate"])
    voice = settings.get("reachy", "voice", "Anna")
    try:
        with tempfile.TemporaryDirectory(prefix="kiyo-speech-") as directory:
            source, wav = Path(directory) / "speech.aiff", Path(directory) / "speech.wav"
            subprocess.run(
                ["/usr/bin/say", "-v", voice, "-o", str(source), "-f", "-"],
                input=text[:4000],
                text=True,
                check=True,
                capture_output=True,
                timeout=45,
            )
            subprocess.run(
                ["/usr/bin/afconvert", "-f", "WAVE", "-d", "LEI16", str(source), str(wav)],
                check=True,
                capture_output=True,
                timeout=10,
            )
            with wave.open(str(wav), "rb") as audio:
                data = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2").astype(np.float32)
                data = data.reshape(-1, audio.getnchannels()).mean(axis=1) / 32768
                input_rate = audio.getframerate()
            if output_rate != input_rate:
                divisor = math.gcd(input_rate, output_rate)
                data = resample_poly(data, output_rate // divisor, input_rate // divisor)
            stereo = np.repeat(data[:, None], min(2, device["max_output_channels"]), axis=1)
            sd.play(stereo, samplerate=output_rate, device=index, blocking=True)
    except (OSError, subprocess.SubprocessError, sd.PortAudioError) as exc:
        raise KiyoError("Kiyo konnte den Text am Reachy Mini nicht ausgeben.") from exc


@contextmanager
def microphone(settings):
    import sounddevice as sd

    index, device = audio_device(settings, sd, "input")
    rate = int(device["default_samplerate"])
    frame_size = round(rate * FRAME / RATE)
    frames = queue.Queue(maxsize=32)

    def capture(indata, _frames, _time, status):
        try:
            frames.put_nowait((indata.copy(), bool(status)))
        except queue.Full:
            pass

    def read():
        try:
            return frames.get(timeout=3)
        except queue.Empty as exc:
            raise KiyoError("Vom Reachy-Mikrofon kommen keine Daten. Prüfe die USB-Verbindung.") from exc

    def drain():
        while not frames.empty():
            frames.get_nowait()

    try:
        with sd.InputStream(
            device=index,
            samplerate=rate,
            channels=min(2, device["max_input_channels"]),
            dtype="float32",
            blocksize=frame_size,
            callback=capture,
        ) as stream:
            yield stream, rate, read, drain
    except sd.PortAudioError as exc:
        raise KiyoError("Das Mikrofon des Reachy Mini ist nicht verfügbar.") from exc


def utterances(read, vad, input_rate):
    import numpy as np
    import torch
    from scipy.signal import resample_poly

    before = deque(maxlen=10)
    speech = []
    divisor = math.gcd(input_rate, RATE)
    while True:
        samples, overflowed = read()
        if overflowed:
            vad.reset_states()
            before.clear()
            speech.clear()
            continue
        mono = samples.mean(axis=1, dtype=np.float32)
        frame = resample_poly(mono, RATE // divisor, input_rate // divisor) if input_rate != RATE else mono
        frame = frame[:FRAME]
        if len(frame) < FRAME:
            frame = np.pad(frame, (0, FRAME - len(frame)))
        vad_frame = np.clip(frame * 4, -1, 1)
        event = vad(torch.from_numpy(vad_frame)) or {}
        if speech:
            speech.append(frame)
            if "end" in event or len(speech) >= 500:
                # Clear the pre-roll before yielding so the next turn starts clean.
                yield np.concatenate(speech)
                vad.reset_states()
                speech.clear()
                before.clear()
        elif "start" in event:
            speech = [*before, frame]
        else:
            before.append(frame)


def listen(settings, assistant):
    try:
        import torch
        from silero_vad import VADIterator, load_silero_vad
    except ImportError as exc:
        raise KiyoError("Installiere die Robotik-Abhängigkeiten mit dem Extra kiyo-voice.") from exc

    root = settings.state
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root / "robot.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise KiyoError("Kiyos Sprachmodus läuft bereits.") from exc

        torch.set_num_threads(1)
        vad = VADIterator(load_silero_vad(), sampling_rate=RATE, min_silence_duration_ms=480)
        active_until = 0.0
        with microphone(settings) as (stream, rate, read, drain):
            read()  # Opening a stream alone does not prove that the device sends samples.
            print("Kiyo hört am Reachy Mini auf seinen Namen.", flush=True)
            for speech in utterances(read, vad, rate):
                stream.stop()
                try:
                    text = transcribe(settings, speech)
                    if not text:
                        continue
                    wake = WAKE.match(text)
                    if wake:
                        text = text[wake.end() :].strip()
                    elif time.monotonic() >= active_until:
                        continue
                    if text.casefold().rstrip(".!?") in SLEEP:
                        speak(settings, "Bis später.")
                        active_until = 0
                        continue
                    print("Sprachanfrage erkannt.", flush=True)
                    try:
                        answer = assistant.ask(text, voice=True) if text else "Ja, ich höre."
                    except KiyoError as exc:
                        answer = str(exc)
                    speak(settings, answer)
                    print("Antwort am Reachy Mini ausgegeben.", flush=True)
                    active_until = time.monotonic() + 45
                finally:
                    drain()
                    stream.start()


def transcribe(settings, speech):
    import mlx_whisper
    import numpy as np

    peak = float(np.max(np.abs(speech)))
    if peak < 0.004:
        return ""
    if peak < 0.4:
        speech = speech * min(0.65 / peak, 15)
    model = settings.get("reachy", "whisper_model", "mlx-community/whisper-base-mlx")
    language = settings.get("reachy", "language", "de")
    result = mlx_whisper.transcribe(
        speech,
        path_or_hf_repo=model,
        language=language,
        verbose=None,
        initial_prompt="Hey Kiyo, sag mir, was heute ansteht." if language == "de" else "Hey Kiyo.",
        condition_on_previous_text=False,
    )
    return result["text"].strip()


def microphone_test(settings, seconds):
    import numpy as np
    from scipy.signal import resample_poly

    print("Jetzt sprechen. Kiyo verarbeitet den Testsatz lokal.", flush=True)
    with microphone(settings) as (_, rate, read, _):
        captured = [read()[0] for _ in range(round(seconds * RATE / FRAME))]
    speech = np.concatenate(captured).mean(axis=1, dtype=np.float32)
    divisor = math.gcd(rate, RATE)
    text = transcribe(settings, resample_poly(speech, RATE // divisor, rate // divisor))
    if not text:
        raise KiyoError("Das Reachy-Mikrofon hat keinen verständlichen Testsatz geliefert.")
    return text
