"""Isotonic calibration of raw model scores, stored as plain arrays (no pickles).

Decisions are made on the RAW score at a fixed-FPR threshold. Isotonic regression is a step
function: it maps many top scores onto the same probability, which would make an FPR-controlled
threshold on calibrated scores coarse. The calibrated probability is reported (score, ECE).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import numpy.typing as npt
from sklearn.isotonic import IsotonicRegression


@dataclass(frozen=True)
class IsotonicCalibrator:
    x: npt.NDArray[np.float64]
    y: npt.NDArray[np.float64]

    @classmethod
    def fit(
        cls, raw: npt.NDArray[np.floating], labels: npt.NDArray[np.integer]
    ) -> IsotonicCalibrator:
        iso = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip", increasing=True)
        iso.fit(np.asarray(raw, dtype=np.float64), np.asarray(labels, dtype=np.float64))
        return cls(
            np.asarray(iso.X_thresholds_, dtype=np.float64),
            np.asarray(iso.y_thresholds_, dtype=np.float64),
        )

    def predict(self, raw: npt.NDArray[np.floating]) -> npt.NDArray[np.float64]:
        """Same as sklearn's IsotonicRegression.predict with out_of_bounds='clip'."""
        out: npt.NDArray[np.float64] = np.interp(np.asarray(raw, dtype=np.float64), self.x, self.y)
        return out

    def save(self, path: Path) -> None:
        np.savez(path, x=self.x, y=self.y)

    @classmethod
    def load(cls, path: Path) -> IsotonicCalibrator:
        with np.load(path, allow_pickle=False) as z:
            return cls(z["x"], z["y"])
