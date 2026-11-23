#!/usr/bin/env python3
"""Fast tabular / micro-net benchmark for the Vajrastra ML scientist.

The template is deliberately tiny: a two-class synthetic tabular problem solved
by logistic regression (a one-layer "micro net") with mini-batch SGD or Adam.
Three seeded runs finish in well under a second on a CPU core, so the MCTS
search can afford a real multi-seed statistical gate.

Heavy libraries are optional and imported lazily.  ``numpy`` accelerates the
inner loop when it is installed (Colab, most Linux desktops); without it the
pure-standard-library implementation runs at the same seeds and produces the
same kind of numbers.  ``torch`` is *not* required -- the CLI stays
zero-dependency on Android.

Usage::

    python templates/tabular_or_micro_net.py --params '{"epochs": 40}' --seed 1
    python templates/tabular_or_micro_net.py --json-file params.json --seed 1 --out run.json

The command prints one JSON object on stdout::

    {"metric": 0.8542, "loss": 0.312, "params": {...}, "seed": 1, ...}
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from typing import Any, Dict, List, Sequence, Tuple

DEFAULT_PARAMS: Dict[str, Any] = {
    "epochs": 6,
    "lr": 0.05,
    "weight_decay": 0.0,
    "batch_size": 32,
    "optimizer": "sgd",
    "standardize": False,
    "hidden_units": 16,
}

#: features are stored at wildly different scales on purpose: a model that
#: standardises them learns much faster, which is what the search discovers.
FEATURE_SCALES: Tuple[float, ...] = (1.0, 5.0, 20.0, 45.0, 120.0, 260.0)
TRUE_WEIGHTS: Tuple[float, ...] = (1.35, -0.55, 1.10, 0.0, -0.95, 0.0)
N_TRAIN = 256
N_VAL = 96


class TemplateError(RuntimeError):
    """Raised when the benchmark cannot run with the supplied parameters."""


def _rng(seed: int) -> random.Random:
    return random.Random(10_000 + int(seed))


def make_dataset(seed: int) -> Tuple[List[Tuple[List[float], int]], List[Tuple[List[float], int]]]:
    """Deterministic, mildly noisy linearly separable dataset."""
    rng = _rng(seed)
    rows: List[Tuple[List[float], int]] = []
    for _ in range(N_TRAIN + N_VAL):
        features = [rng.gauss(0.0, 1.0) * scale for scale in FEATURE_SCALES]
        signal = sum(
            weight * value / scale
            for weight, value, scale in zip(TRUE_WEIGHTS, features, FEATURE_SCALES)
        )
        signal += 0.45 * rng.gauss(0.0, 1.0)
        rows.append((features, 1 if signal > 0.0 else 0))
    return rows[:N_TRAIN], rows[N_TRAIN:]


def _standardise(rows: Sequence[Tuple[List[float], int]], dim: int) -> Tuple[List[float], List[float]]:
    means = [0.0] * dim
    squares = [0.0] * dim
    for features, _ in rows:
        for index, value in enumerate(features):
            means[index] += value
            squares[index] += value * value
    count = float(len(rows) or 1)
    means = [value / count for value in means]
    stdevs = []
    for index, total in enumerate(squares):
        variance = max(total / count - means[index] ** 2, 0.0)
        stdevs.append(math.sqrt(variance) or 1.0)
    return means, stdevs


def _apply(features: Sequence[float], means: Sequence[float], stdevs: Sequence[float]) -> List[float]:
    return [(value - mean) / stdev for value, mean, stdev in zip(features, means, stdevs)]


def _sigmoid(z: float) -> float:
    if z < -60.0:
        return 0.0
    if z > 60.0:
        return 1.0
    return 1.0 / (1.0 + math.exp(-z))


def _accuracy(
    rows: Sequence[Tuple[List[float], int]],
    weights: Sequence[float],
    bias: float,
    means: Sequence[float],
    stdevs: Sequence[float],
) -> Tuple[float, float]:
    correct = 0
    loss = 0.0
    for features, label in rows:
        vector = _apply(features, means, stdevs)
        z = bias + sum(weight * value for weight, value in zip(weights, vector))
        probability = _sigmoid(z)
        clipped = min(max(probability, 1e-9), 1.0 - 1e-9)
        loss -= math.log(clipped) if label else math.log(1.0 - clipped)
        if (probability >= 0.5) == bool(label):
            correct += 1
    count = float(len(rows) or 1)
    return correct / count, loss / count


def train_stdlib(params: Dict[str, Any], seed: int) -> Dict[str, Any]:
    """Pure-standard-library trainer; deterministic for a given seed."""
    train, validation = make_dataset(seed)
    dim = len(FEATURE_SCALES)
    if params["standardize"]:
        means, stdevs = _standardise(train, dim)
    else:
        means, stdevs = [0.0] * dim, [1.0] * dim

    vectors = [(_apply(features, means, stdevs), label) for features, label in train]
    weights = [0.0] * dim
    bias = 0.0
    lr = float(params["lr"])
    decay = float(params["weight_decay"])
    batch_size = max(1, int(params["batch_size"]))
    epochs = max(1, int(params["epochs"]))
    adam = str(params["optimizer"]).lower() == "adam"
    m_w, v_w = [0.0] * dim, [0.0] * dim
    m_b, v_b = 0.0, 0.0
    beta1, beta2, eps = 0.9, 0.999, 1e-8
    step = 0
    order = list(range(len(vectors)))
    rng = _rng(seed + 7)

    for epoch in range(epochs):
        rng.shuffle(order)
        for start in range(0, len(order), batch_size):
            chunk = order[start : start + batch_size]
            grad_w = [0.0] * dim
            grad_b = 0.0
            for index in chunk:
                vector, label = vectors[index]
                z = bias + sum(weight * value for weight, value in zip(weights, vector))
                error = _sigmoid(z) - float(label)
                for position, value in enumerate(vector):
                    grad_w[position] += error * value
                grad_b += error
            scale = 1.0 / float(len(chunk))
            step += 1
            for position in range(dim):
                gradient = grad_w[position] * scale + decay * weights[position]
                if adam:
                    m_w[position] = beta1 * m_w[position] + (1.0 - beta1) * gradient
                    v_w[position] = beta2 * v_w[position] + (1.0 - beta2) * gradient * gradient
                    m_hat = m_w[position] / (1.0 - beta1 ** step)
                    v_hat = v_w[position] / (1.0 - beta2 ** step)
                    weights[position] -= lr * m_hat / (math.sqrt(v_hat) + eps)
                else:
                    weights[position] -= lr * gradient
            gradient = grad_b * scale
            if adam:
                m_b = beta1 * m_b + (1.0 - beta1) * gradient
                v_b = beta2 * v_b + (1.0 - beta2) * gradient * gradient
                m_hat = m_b / (1.0 - beta1 ** step)
                v_hat = v_b / (1.0 - beta2 ** step)
                bias -= lr * m_hat / (math.sqrt(v_hat) + eps)
            else:
                bias -= lr * gradient

    accuracy, loss = _accuracy(validation, weights, bias, means, stdevs)
    train_accuracy, _ = _accuracy(train, weights, bias, means, stdevs)
    return {
        "metric": round(accuracy, 6),
        "train_metric": round(train_accuracy, 6),
        "loss": round(loss, 6),
        "epochs": epochs,
        "engine": "stdlib",
    }


def train_numpy(params: Dict[str, Any], seed: int) -> Dict[str, Any]:
    """Same experiment through numpy when it is installed."""
    import numpy as np  # noqa: PLC0415 - lazy: Android installs have no numpy

    rng_local = _rng(seed)
    rows = make_dataset(seed)
    train_rows, validation_rows = rows

    train_x = np.array([features for features, _ in train_rows], dtype=float)
    train_y = np.array([label for _, label in train_rows], dtype=float)
    validation_x = np.array([features for features, _ in validation_rows], dtype=float)
    validation_y = np.array([label for _, label in validation_rows], dtype=float)
    if params["standardize"]:
        means = train_x.mean(axis=0)
        stdevs = train_x.std(axis=0)
        stdevs[stdevs == 0] = 1.0
        train_x = (train_x - means) / stdevs
        validation_x = (validation_x - means) / stdevs

    weights = np.zeros(train_x.shape[1])
    bias = 0.0
    m_w = np.zeros_like(weights)
    v_w = np.zeros_like(weights)
    m_b = 0.0
    v_b = 0.0
    beta1, beta2, eps = 0.9, 0.999, 1e-8
    lr = float(params["lr"])
    decay = float(params["weight_decay"])
    batch_size = max(1, int(params["batch_size"]))
    epochs = max(1, int(params["epochs"]))
    adam = str(params["optimizer"]).lower() == "adam"
    permutation = np.arange(len(train_x))
    step = 0

    def sigmoid(values):
        return 1.0 / (1.0 + np.exp(-np.clip(values, -60.0, 60.0)))

    for _ in range(epochs):
        np.random.RandomState(rng_local.randrange(1 << 31)).shuffle(permutation)
        for start in range(0, len(permutation), batch_size):
            chunk = permutation[start : start + batch_size]
            batch_x = train_x[chunk]
            batch_y = train_y[chunk]
            error = sigmoid(batch_x @ weights + bias) - batch_y
            grad_w = batch_x.T @ error / len(chunk) + decay * weights
            grad_b = float(error.mean())
            step += 1
            if adam:
                m_w = beta1 * m_w + (1 - beta1) * grad_w
                v_w = beta2 * v_w + (1 - beta2) * grad_w**2
                weights -= lr * (m_w / (1 - beta1**step)) / (np.sqrt(v_w / (1 - beta2**step)) + eps)
                m_b = beta1 * m_b + (1 - beta1) * grad_b
                v_b = beta2 * v_b + (1 - beta2) * grad_b**2
                bias -= lr * (m_b / (1 - beta1**step)) / (math.sqrt(v_b / (1 - beta2**step)) + eps)
            else:
                weights -= lr * grad_w
                bias -= lr * grad_b

    probabilities = sigmoid(validation_x @ weights + bias)
    predictions = (probabilities >= 0.5).astype(float)
    accuracy = float((predictions == validation_y).mean())
    clipped = np.clip(probabilities, 1e-9, 1 - 1e-9)
    loss = float(-(validation_y * np.log(clipped) + (1 - validation_y) * np.log(1 - clipped)).mean())
    train_predictions = (sigmoid(train_x @ weights + bias) >= 0.5).astype(float)
    return {
        "metric": round(accuracy, 6),
        "train_metric": round(float((train_predictions == train_y).mean()), 6),
        "loss": round(loss, 6),
        "epochs": epochs,
        "engine": "numpy",
    }


def run(params: Dict[str, Any], seed: int) -> Dict[str, Any]:
    """Validate parameters, train, and return the result payload."""
    merged = dict(DEFAULT_PARAMS)
    merged.update(params or {})
    if int(merged["epochs"]) < 1:
        raise TemplateError("epochs must be >= 1")
    if float(merged["lr"]) <= 0.0:
        raise TemplateError("lr must be positive")
    started = time.perf_counter()
    try:
        payload = train_numpy(merged, seed)
    except ImportError:
        payload = train_stdlib(merged, seed)
    payload.update(
        {
            "params": merged,
            "seed": int(seed),
            "duration_s": round(time.perf_counter() - started, 6),
            "n_train": N_TRAIN,
            "n_val": N_VAL,
        }
    )
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Vajrastra fast benchmark template")
    parser.add_argument("--params", default="{}", help="JSON object of hyper-parameters")
    parser.add_argument("--json-file", default=None, help="read parameters from a JSON file")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", default=None, help="also write the result JSON here")
    args = parser.parse_args(argv)

    params: Dict[str, Any] = {}
    if args.json_file:
        params.update(json.loads(open(args.json_file, encoding="utf-8").read()))
    if args.params:
        params.update(json.loads(args.params))

    try:
        payload = run(params, args.seed)
    except (TemplateError, ValueError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    text = json.dumps(payload, sort_keys=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
