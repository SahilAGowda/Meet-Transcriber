"""Run the production CPU transcription + diarization path against a mono WAV file."""
from __future__ import annotations

import argparse
from datetime import datetime

from app.audio import read_wav
from app.config import MEETINGS_DIR
from app.diarization import LocalSpeakerTracker
from app.service import Transcriber
from app.transcript import TranscriptWriter


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("wav", help="mono 16-bit PCM WAV input")
    parser.add_argument("--chunk-seconds", type=float, default=1.5)
    args = parser.parse_args()
    audio = read_wav(args.wav)
    started = datetime.now()
    writer = TranscriptWriter(MEETINGS_DIR / f"meeting_{started:%Y-%m-%d_%H-%M-%S}.md", started)
    recognizer, speakers = Transcriber(), LocalSpeakerTracker()
    step = int(args.chunk_seconds * audio.sample_rate)
    previous = ""
    from app.service import deduplicate
    for offset in range(0, len(audio.samples), step):
        chunk = audio.samples[offset:offset + step]
        text = deduplicate(previous, recognizer.transcribe(type(audio)(chunk, audio.sample_rate))).strip()
        if text:
            writer.append(offset / audio.sample_rate, speakers.label(chunk), text)
            previous = text
    print(writer.path)


if __name__ == "__main__":
    main()

