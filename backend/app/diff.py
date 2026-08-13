"""Pixel diff / SSIM maps for stored comparison frames."""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Literal

import cv2
import numpy as np
from skimage.metrics import structural_similarity as ssim_fn

DiffMode = Literal["absdiff", "ssim_map"]

MAX_DECODED_BYTES = 10 * 1024 * 1024
MAX_DIMENSION = 4096


def _load_png(path: Path) -> np.ndarray:
    data = path.read_bytes()
    if len(data) > MAX_DECODED_BYTES:
        raise ValueError("Image too large")
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Could not decode image")
    h, w = img.shape[:2]
    if w > MAX_DIMENSION or h > MAX_DIMENSION:
        raise ValueError("Image dimensions too large")
    return img


def encode_image_b64(img: np.ndarray) -> str:
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise ValueError("Could not encode image")
    return base64.b64encode(buf.tobytes()).decode("ascii")


def align_sizes(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if a.shape[:2] == b.shape[:2]:
        return a, b
    b_resized = cv2.resize(b, (a.shape[1], a.shape[0]), interpolation=cv2.INTER_AREA)
    return a, b_resized


def compute_mse(a: np.ndarray, b: np.ndarray) -> float:
    a_f = a.astype(np.float64)
    b_f = b.astype(np.float64)
    return float(np.mean((a_f - b_f) ** 2))


def compute_psnr(mse: float, max_value: float = 255.0) -> float:
    """Peak signal-to-noise ratio in dB from MSE (8-bit → max 255)."""
    if mse <= 0:
        return float("inf")
    return float(10.0 * np.log10((max_value * max_value) / mse))


def compute_ssim(a: np.ndarray, b: np.ndarray) -> float:
    gray_a = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)
    gray_b = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY)
    score, _ = ssim_fn(gray_a, gray_b, full=True)
    return float(score)


def absdiff_map(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    a, b = align_sizes(a, b)
    diff = cv2.absdiff(a, b)
    gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
    return cv2.applyColorMap(gray, cv2.COLORMAP_JET)


def ssim_disparity_map(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    a, b = align_sizes(a, b)
    gray_a = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)
    gray_b = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY)
    _, ssim_map = ssim_fn(gray_a, gray_b, full=True)
    dissim = (1.0 - ssim_map).clip(0.0, 1.0)
    dissim_u8 = (dissim * 255).astype(np.uint8)
    return cv2.applyColorMap(dissim_u8, cv2.COLORMAP_INFERNO)


def _metrics_only(a: np.ndarray, b: np.ndarray) -> dict:
    a, b = align_sizes(a, b)
    mse = compute_mse(a, b)
    ssim_score = compute_ssim(a, b)
    psnr = compute_psnr(mse)
    return {
        "mse": mse,
        "ssim": ssim_score,
        "psnr": psnr if psnr != float("inf") else None,
    }


def compare_png_bytes(source_png: bytes, dest_png: bytes) -> dict:
    """SSIM / PSNR / MSE for in-memory PNG pair (no map image)."""
    if len(source_png) > MAX_DECODED_BYTES or len(dest_png) > MAX_DECODED_BYTES:
        raise ValueError("Image too large")
    a = cv2.imdecode(np.frombuffer(source_png, dtype=np.uint8), cv2.IMREAD_COLOR)
    b = cv2.imdecode(np.frombuffer(dest_png, dtype=np.uint8), cv2.IMREAD_COLOR)
    if a is None or b is None:
        raise ValueError("Could not decode image")
    if a.shape[1] > MAX_DIMENSION or a.shape[0] > MAX_DIMENSION:
        raise ValueError("Image dimensions too large")
    if b.shape[1] > MAX_DIMENSION or b.shape[0] > MAX_DIMENSION:
        raise ValueError("Image dimensions too large")
    return _metrics_only(a, b)


def compare_frame_files(
    source_png: Path,
    dest_png: Path,
    mode: DiffMode = "absdiff",
) -> dict:
    a = _load_png(source_png)
    b = _load_png(dest_png)
    a, b = align_sizes(a, b)

    metrics = _metrics_only(a, b)

    if mode == "ssim_map":
        result = ssim_disparity_map(a, b)
    else:
        result = absdiff_map(a, b)

    return {
        "image": encode_image_b64(result),
        **metrics,
    }
