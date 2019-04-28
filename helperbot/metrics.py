from typing import Tuple

import torch
import numpy as np
from sklearn.metrics import fbeta_score, roc_auc_score


class Metric:
    name = "metric"

    def __call__(self, truth: torch.Tensor, pred: torch.Tensor) -> Tuple[float, str]:
        raise NotImplementedError()


class FBeta(Metric):
    """FBeta for binary targets"""
    name = "fbeta"

    def __init__(self, step, beta=2):
        self.step = step
        self.beta = beta

    def __call__(self, truth: torch.Tensor, pred: torch.Tensor) -> Tuple[float, str]:
        best_fbeta, best_thres = self.find_best_fbeta_threshold(
            truth.numpy(), torch.sigmoid(pred).numpy(),
            step=self.step, beta=self.beta)
        return best_fbeta * -1, f"{best_fbeta:.4f} @ {best_thres:.2f}"

    @staticmethod
    def find_best_fbeta_threshold(truth, probs, beta=2, step=0.05):
        best, best_thres = 0, -1
        for thres in np.arange(step, 1, step):
            current = fbeta_score(
                truth, (probs >= thres).astype("int8"), beta=beta)
            if current > best:
                best = current
                best_thres = thres
        return best, best_thres


class AUC(Metric):
    """AUC for binary targets"""
    name = "auc"

    def __call__(self, truth: torch.Tensor, pred: torch.Tensor) -> Tuple[float, str]:
        auc_score = roc_auc_score(
            truth.numpy(), torch.sigmoid(pred).numpy())
        return auc_score * -1, f"{auc_score * 100:.2f}"
