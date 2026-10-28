# -*- coding: utf-8 -*-
"""不依赖 Blender / GGUF 权重的单元测试。

覆盖：
  1. sd-cli 日志解析（耗时数字与采样器名）
  2. 白底抠图 head_alpha（椭圆约束 + 背景 key）
  3. 预乘 alpha 缩放（不产生白边）
  4. 几何一致性指标 chamfer（偏移量可被正确测出）
  5. 流水线的 control image RGB 兜底
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import compose_head  # noqa: E402
import run_pipeline  # noqa: E402

SAMPLE_LOG = """
[I] sampling using Euler A method --- request.cpp:477
[I] control_net build cached graph cut plan done (taking 1 ms) --- ggml_graph_cut.cpp:1034

  |==================================================| 4/4 -  21.90s/it
[I] sampling completed, taking 87.59s --- image.cpp:953
[I] generating 1 latent images completed, taking 87.60s --- image.cpp:967
[I] decoding 1 latents --- image.cpp:605
[I] latent 1 decoded, taking 35.69s --- image.cpp:671
[I] decode_first_stage completed, taking 35.69s --- image.cpp:675
[I] generate_image completed in 143.81s --- image.cpp:1140
"""


def test_parse_sdcpp_log_extracts_timings():
    info = run_pipeline.parse_sdcpp_log(SAMPLE_LOG)
    assert info["sampling_s"] == pytest.approx(87.59)
    assert info["vae_s"] == pytest.approx(35.69)
    assert info["total_reported_s"] == pytest.approx(143.81)
    assert info["it_per_step_s"] == pytest.approx(21.90)
    assert info["sampler"].lower() == "euler a"


def test_parse_sdcpp_log_is_robust_to_garbage():
    assert run_pipeline.parse_sdcpp_log("nothing here") == {}


def _white_bg_portrait(w=200, h=240):
    """白底 + 肤色椭圆“脸”：模拟 compose_head 的输入。"""
    img = np.full((h, w, 3), 255, np.uint8)
    cv2.ellipse(img, (w // 2, h // 2), (w // 4, h // 3), 0, 0, 360, (120, 160, 220), -1)
    return img


def test_head_alpha_keys_white_background():
    img = _white_bg_portrait()
    face = (0.2 * img.shape[1], 0.15 * img.shape[0], 0.8 * img.shape[1], 0.85 * img.shape[0])
    alpha = compose_head.head_alpha(img, face)
    assert alpha.shape == img.shape[:2]
    # 中心（脸）不透明、四角（白底）透明
    assert alpha[img.shape[0] // 2, img.shape[1] // 2] > 0.8
    assert alpha[0, 0] < 0.05 and alpha[-1, -1] < 0.05


def test_resize_premul_avoids_white_fringe():
    """预乘缩放后，边缘像素不应被白底插值成亮色（这是“白边”的成因）。"""
    rgb = _white_bg_portrait()
    face = (0.2 * rgb.shape[1], 0.15 * rgb.shape[0], 0.8 * rgb.shape[1], 0.85 * rgb.shape[0])
    alpha = compose_head.head_alpha(rgb, face)
    small, a, premul = compose_head.resize_premul(rgb, alpha, (60, 72))
    edge = (a > 0.1) & (a < 0.9)
    if edge.sum() > 0:
        # 半透明边缘的颜色应接近脸的颜色，而不是白
        assert float(small[edge].mean()) < 240.0
    # 预乘缓冲在 alpha=0 处必须是 0（premul 为 0~255 尺度，允许 1 个色阶的插值残留）
    assert float(premul[a < 1e-3].max(initial=0.0)) <= 1.0


def test_chamfer_detects_shift():
    """把参考边缘整体右移 3px，倒角距离应接近 3px。"""
    import analyze_runs

    ref = np.zeros((128, 128), np.uint8)
    cv2.rectangle(ref, (30, 30), (90, 90), 255, -1)
    test = np.zeros_like(ref)
    cv2.rectangle(test, (33, 33), (93, 93), 255, -1)   # 整体右移 + 下移 3px

    tmp = Path(__file__).parent / "_tmp"
    tmp.mkdir(exist_ok=True)
    cv2.imwrite(str(tmp / "ref.png"), ref)
    cv2.imwrite(str(tmp / "test.png"), test)
    res = analyze_runs.chamfer(tmp / "ref.png", tmp / "test.png", None)
    assert res
    assert 2.5 <= res["chamfer_median_px"] <= 4.5
    assert res["coverage_4px"] > 0.9


def test_ensure_rgb_converts_grayscale(tmp_path):
    p = tmp_path / "depth.png"
    cv2.imwrite(str(p), np.full((32, 32), 128, np.uint8))
    run_pipeline.ensure_rgb(p)
    with open(p, "rb") as fh:
        header = fh.read(26)
    # PNG IHDR 的 color type 位于第 25 字节：2 表示 RGB
    assert header[25] == 2
