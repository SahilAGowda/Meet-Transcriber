from __future__ import annotations

import numpy as np


class LocalSpeakerTracker:
    """CPU-only online acoustic clustering; labels voices, never identifies people.

    This lightweight MVP avoids cloud services and GPU-only diarization. It clusters
    voiced chunks by spectral envelope and energy. Overlapping speakers cannot be
    separated perfectly; the transcript documents that limitation.
    """
    def __init__(self, threshold: float = 0.22) -> None:
        self.centroids: list[np.ndarray] = []
        self.counts: list[int] = []
        self.threshold = threshold

    @staticmethod
    def _feature(samples: np.ndarray) -> np.ndarray | None:
        if len(samples) < 400 or float(np.sqrt(np.mean(samples ** 2))) < 0.008:
            return None
        window = np.hanning(min(4096, len(samples)))
        spectrum = np.abs(np.fft.rfft(samples[: len(window)] * window)) + 1e-7
        bins = np.array_split(np.log(spectrum), 16)
        vector = np.array([part.mean() for part in bins], dtype=np.float32)
        return (vector - vector.mean()) / (vector.std() + 1e-6)

    def label(self, samples: np.ndarray) -> str:
        feature = self._feature(samples)
        if feature is None:
            return "Speaker 1" if self.centroids else "Speaker 1"
        if not self.centroids:
            self.centroids.append(feature); self.counts.append(1)
            return "Speaker 1"
        distances = [float(np.linalg.norm(feature - point) / np.sqrt(len(feature))) for point in self.centroids]
        index = int(np.argmin(distances))
        if distances[index] > self.threshold:
            self.centroids.append(feature); self.counts.append(1)
            index = len(self.centroids) - 1
        else:
            count = self.counts[index]
            self.centroids[index] = (self.centroids[index] * count + feature) / (count + 1)
            self.counts[index] += 1
        return f"Speaker {index + 1}"

