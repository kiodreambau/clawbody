"""Reachy Mini USB audio with local VAD, Whisper and macOS speech synthesis."""

import fcntl
import math
import os
import re
import subprocess
import tempfile
import time
import wave
from collections import deque
from pathlib import Path

from .settings import KiyoError

RATE = 16000
FRAME = 512
WAKE = re.compile(r"^(?:hey[ ,]+)?(?:kiyo|kio|kijo|kyo|kiko)\b[\s,.!?;:–-]*", re.IGNORECASE)
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


def utterances(stream, vad, input_rate, frame_size):
    import numpy as np
    import torch
    from scipy.signal import resample_poly

    before = deque(maxlen=10)
    speech = []
    divisor = math.gcd(input_rate, RATE)
    while True:
        samples, overflowed = stream.read(frame_size)
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
        event = vad(torch.from_numpy(frame)) or {}
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
        import sounddevice as sd
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
        index, device = audio_device(settings, sd, "input")
        input_rate = int(device["default_samplerate"])
        frame_size = round(input_rate * FRAME / RATE)
        active_until = 0.0
        try:
            with sd.InputStream(
                device=index, samplerate=input_rate, channels=1, dtype="float32", blocksize=frame_size
            ) as stream:
                print("Kiyo hört am Reachy Mini auf seinen Namen.", flush=True)
                for speech in utterances(stream, vad, input_rate, frame_size):
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
                        stream.start()
        except sd.PortAudioError as exc:
            raise KiyoError("Das Mikrofon des Reachy Mini ist nicht verfügbar.") from exc


def transcribe(settings, speech):
    import mlx_whisper

    model = settings.get("reachy", "whisper_model", "mlx-community/whisper-base-mlx")
    language = settings.get("reachy", "language", "de")
    result = mlx_whisper.transcribe(
        speech,
        path_or_hf_repo=model,
        language=language,
        verbose=None,
        condition_on_previous_text=False,
    )
    return result["text"].strip()


def microphone_test(settings, seconds):
    import numpy as np
    import sounddevice as sd
    from scipy.signal import resample_poly

    index, device = audio_device(settings, sd, "input")
    input_rate = int(device["default_samplerate"])
    print("Jetzt sprechen. Kiyo verarbeitet den Testsatz lokal.", flush=True)
    try:
        speech = sd.rec(
            input_rate * seconds, samplerate=input_rate, channels=1, dtype="float32", device=index, blocking=True
        ).ravel()
    except sd.PortAudioError as exc:
        raise KiyoError("Das Mikrofon des Reachy Mini ist nicht verfügbar.") from exc
    if np.max(np.abs(speech)) < 0.01:
        raise KiyoError("Das Reachy-Mikrofon hat kein deutliches Sprachsignal erkannt.")
    divisor = math.gcd(input_rate, RATE)
    return transcribe(settings, resample_poly(speech, RATE // divisor, input_rate // divisor))
