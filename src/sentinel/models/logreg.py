"""Logistic-regression baseline: signed log1p -> standardise -> clip -> L2 logistic regression.

Exported as plain arrays so scoring is a dot product (no pickles, no sklearn at serving time).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import numpy.typing as npt
from sklearn.linear_model import LogisticRegression

from sentinel.config import LogRegConfig


def _log_copy(x: npt.NDArray[np.floating], signed_log1p: bool) -> npt.NDArray[np.float64]:
    """float64 copy, optionally sign(x)*log1p(|x|), computed in place (one copy of the data)."""
    z = np.array(x, dtype=np.float64)
    if signed_log1p:
        negative = z < 0
        np.abs(z, out=z)
        np.log1p(z, out=z)
        z[negative] *= -1.0
    return z


@dataclass(frozen=True)
class LogRegModel:
    mean: npt.NDArray[np.float64]
    scale: npt.NDArray[np.float64]
    coef: npt.NDArray[np.float64]
    intercept: float
    signed_log1p: bool
    clip: float
    converged: bool = True

    def _transform(self, x: npt.NDArray[np.floating]) -> npt.NDArray[np.float64]:
        z = _log_copy(x, self.signed_log1p)
        z -= self.mean
        z /= self.scale
        np.clip(z, -self.clip, self.clip, out=z)
        return z

    def predict(self, x: npt.NDArray[np.floating]) -> npt.NDArray[np.float64]:
        """P(malicious) for each row."""
        logits = self._transform(x) @ self.coef + self.intercept
        out: npt.NDArray[np.float64] = 1.0 / (1.0 + np.exp(-logits))
        return out

    @classmethod
    def fit(
        cls,
        x: npt.NDArray[np.floating],
        y: npt.NDArray[np.integer],
        cfg: LogRegConfig,
        seed: int,
    ) -> LogRegModel:
        z = _log_copy(x, cfg.signed_log1p)
        mean = z.mean(axis=0)
        scale = z.std(axis=0)
        scale[scale == 0] = 1.0  # constant in the subsample: leave centred at 0
        z -= mean
        z /= scale
        np.clip(z, -cfg.clip, cfg.clip, out=z)
        clf = LogisticRegression(C=cfg.C, max_iter=cfg.max_iter, random_state=seed)
        clf.fit(z, np.asarray(y))
        return cls(
            mean=mean,
            scale=scale,
            coef=np.asarray(clf.coef_[0], dtype=np.float64),
            intercept=float(clf.intercept_[0]),
            signed_log1p=cfg.signed_log1p,
            clip=cfg.clip,
            converged=bool(int(np.max(clf.n_iter_)) < cfg.max_iter),
        )

    def save(self, path: Path) -> None:
        np.savez(
            path,
            mean=self.mean,
            scale=self.scale,
            coef=self.coef,
            intercept=np.array([self.intercept]),
            signed_log1p=np.array([self.signed_log1p]),
            clip=np.array([self.clip]),
            converged=np.array([self.converged]),
        )

    @classmethod
    def load(cls, path: Path) -> LogRegModel:
        with np.load(path, allow_pickle=False) as z:
            return cls(
                mean=z["mean"],
                scale=z["scale"],
                coef=z["coef"],
                intercept=float(z["intercept"][0]),
                signed_log1p=bool(z["signed_log1p"][0]),
                clip=float(z["clip"][0]),
                converged=bool(z["converged"][0]),
            )
