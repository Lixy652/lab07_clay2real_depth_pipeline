#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把一张正面头像融合进生成的人物图（拓展项）。

链路（与实验指南一致）：
    深度图定位头部框 → 等比缩放头像 → 白底软抠图 + 椭圆约束 + 内缩 1px 羽化
    → 预乘 alpha 贴图 → 亮度对齐 → 接触阴影 → 梯度域（Poisson）融合 → 颗粒匹配

用法：
    python scripts/compose_head.py --base output/headfig_photoreal.png \
        --head assets/portrait.png --depth output/headfig_depth.png \
        --out output/headfig_face.png

参数（默认值与指南一致）：
    --face 0.16,0.02,0.85,0.71   头像内人脸框比例 left,top,right,chin
    --box-grow 0.13,0.14,0.13,0.06  头部框外扩比例（左,上,右,下）
    --clone 0.6                  Poisson 融合占比，0 为纯贴图
    --shadow 0.35                下巴与脖子的接触阴影强度
    --grain 1.0                  颗粒噪声匹配强度
    --warp-fill                  用横向拉伸的头像填充头框
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np


# --------------------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------------------
def parse_floats(text, n):
    vals = [float(v) for v in str(text).split(",")]
    if len(vals) != n:
        raise argparse.ArgumentTypeError("需要 %d 个逗号分隔的数值，收到：%s" % (n, text))
    return vals


def head_box_from_depth(depth, fallback_ratio=(0.30, 0.02, 0.70, 0.42), top_frac=0.62):
    """用深度图定位头部。

    深度图“近亮远暗”，但画面里最亮的往往不是头：可能是最靠近相机的地面，也可能是抬起的
    手臂或手。站立人形的头部有两个稳定特征——**位置最高**、**形状紧凑（近似方/圆）**，
    所以只在画面上部 top_frac 区域取“近处表面”的亮团，然后按
        score = 紧凑度 − 0.6·(顶边高度) − 0.3·(横向偏心)
    打分：先保证“最高”，再用紧凑度和居中做二次排序。头部尺寸限制在画面 0.3%~8%。
    """
    h, w = depth.shape
    blur = cv2.GaussianBlur(depth, (0, 0), 3.0)
    top = blur[: int(h * top_frac)]
    thr = float(np.percentile(top, 97.0))
    mask = (blur >= thr).astype(np.uint8)
    mask[int(h * top_frac):] = 0
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    n, labels, stats, cent = cv2.connectedComponentsWithStats(mask, 8)
    best, best_score = None, -1e9
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        if area < 0.003 * w * h or area > 0.08 * w * h:
            continue
        if bw > 0.40 * w or bh > 0.45 * h:      # 太大：躯干/地面
            continue
        if y + bh > 0.985 * h:                  # 贴底：地面
            continue
        aspect = bw / float(max(bh, 1))         # 头接近方形，长条多半是手臂
        if not (0.45 <= aspect <= 2.0):
            continue
        compact = area / float(bw * bh)
        cx = cent[i][0] / w
        score = compact - 0.6 * (y / float(h)) - 0.3 * abs(cx - 0.5)
        if score > best_score:
            best_score, best = score, (x, y, x + bw, y + bh)
    if best is None:
        best = (int(fallback_ratio[0] * w), int(fallback_ratio[1] * h),
                int(fallback_ratio[2] * w), int(fallback_ratio[3] * h))
    return [int(v) for v in best]


def head_alpha(rgb, face, ellipse_soft=3.0):
    """白底软抠图，再用椭圆限制成只留头部与少量脖子。"""
    key = np.clip((255.0 - rgb.min(axis=2) - 6.0) / 18.0, 0.0, 1.0)
    left, top, right, chin = face
    cx, cy = (left + right) / 2.0, (top + chin) / 2.0 + (chin - top) * 0.03
    rx, ry = (right - left) / 2.0 * 1.05, (chin - top) / 2.0 * 1.06
    emask = np.zeros(rgb.shape[:2], np.uint8)
    cv2.ellipse(emask, (int(cx), int(cy)), (int(rx), int(ry)), 0, 0, 360, 255, -1)
    emask = cv2.GaussianBlur(emask, (0, 0), ellipse_soft).astype(np.float32) / 255.0
    alpha = np.clip(key * emask, 0.0, 1.0)
    hard = (alpha > 0.55).astype(np.uint8)
    hard = cv2.erode(hard, np.ones((3, 3), np.uint8), iterations=1)
    return cv2.GaussianBlur(hard.astype(np.float32), (0, 0), 1.2)


def resize_premul(rgb, alpha, size):
    """先乘 alpha 再缩放，避免白底像素在边缘被插值成亮边。"""
    a = cv2.resize(alpha, size, interpolation=cv2.INTER_AREA)
    p = cv2.resize(rgb.astype(np.float32) * alpha[..., None], size,
                   interpolation=cv2.INTER_AREA)
    safe = np.maximum(a[..., None], 1e-3)
    un = np.where(a[..., None] > 1e-3, p / safe, 0.0)
    return np.clip(un, 0, 255).astype(np.uint8), a, p


def contact_shadow(base, box, face_ratio, strength):
    """下巴下方的椭圆软阴影，让头与脖子“接触”而不是浮在表面。"""
    if strength <= 0:
        return base
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    cx = (x0 + x1) / 2.0
    cy = y0 + h * (0.80 + 0.10 * face_ratio)
    rx, ry = w * 0.34, h * 0.14
    yy, xx = np.mgrid[0:base.shape[0], 0:base.shape[1]].astype(np.float32)
    d = ((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2
    fall = np.clip(1.0 - d, 0.0, 1.0) ** 1.5
    shadow = 1.0 - strength * fall[..., None]
    return np.clip(base.astype(np.float32) * shadow, 0, 255)


def match_grain(img, amount, region_mask):
    """匹配颗粒噪点：估计区域内高频标准差，再叠加同量级高斯噪声。"""
    if amount <= 0:
        return img
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    high = gray - cv2.GaussianBlur(gray, (0, 0), 2.0)
    sel = region_mask > 0
    if sel.sum() < 100:
        return img
    sigma = float(np.std(high[sel]))
    sigma = min(max(sigma, 1.0), 12.0)
    rng = np.random.default_rng(1234)
    noise = rng.normal(0.0, sigma * amount, img.shape[:2]).astype(np.float32)
    out = img.astype(np.float32) + noise[..., None]
    return np.clip(out, 0, 255)


# --------------------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------------------
def main():
    try:  # 控制台可能是 GBK，避免打印 ² 等字符时崩溃
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="compose a portrait head into a generated figure")
    ap.add_argument("--base", required=True, help="生成的人物图（photoreal）")
    ap.add_argument("--head", required=True, help="正面头像照片（白底最佳）")
    ap.add_argument("--depth", required=True, help="与 base 对应的深度图（用于定位头部）")
    ap.add_argument("--out", required=True, help="输出融合图")
    ap.add_argument("--face", default="0.16,0.02,0.85,0.71",
                    help="头像内人脸框比例 left,top,right,chin")
    ap.add_argument("--box-grow", default="0.13,0.14,0.13,0.06",
                    help="头部框外扩比例 左,上,右,下")
    ap.add_argument("--clone", type=float, default=0.6, help="Poisson 融合占比 0~1")
    ap.add_argument("--shadow", type=float, default=0.35, help="接触阴影强度")
    ap.add_argument("--grain", type=float, default=1.0, help="颗粒噪声匹配强度")
    ap.add_argument("--warp-fill", action="store_true", help="横向拉伸头像填满头框")
    ap.add_argument("--brightness", type=float, default=1.0, help="亮度对齐强度 0~1")
    ap.add_argument("--debug-dir", default=None, help="可选：保存中间 mask/贴图便于排查")
    args = ap.parse_args()

    base = cv2.imread(str(args.base), cv2.IMREAD_COLOR)
    head = cv2.imread(str(args.head), cv2.IMREAD_COLOR)
    depth = cv2.imread(str(args.depth), cv2.IMREAD_GRAYSCALE)
    if base is None or head is None or depth is None:
        raise SystemExit("读图失败：base/head/depth 路径是否正确？")
    if depth.shape[:2] != base.shape[:2]:
        depth = cv2.resize(depth, (base.shape[1], base.shape[0]), interpolation=cv2.INTER_LINEAR)

    # 1) 深度图定位头部框 + 外扩
    bx0, by0, bx1, by1 = head_box_from_depth(depth)
    bw, bh = bx1 - bx0, by1 - by0
    g = parse_floats(args.box_grow, 4)
    box = [int(round(bx0 - g[0] * bw)), int(round(by0 - g[1] * bh)),
           int(round(bx1 + g[2] * bw)), int(round(by1 + g[3] * bh))]
    box[0] = max(0, box[0]); box[1] = max(0, box[1])
    box[2] = min(base.shape[1] - 1, box[2]); box[3] = min(base.shape[0] - 1, box[3])
    if box[2] - box[0] < 16 or box[3] - box[1] < 16:
        raise SystemExit("头部框太小（%s），请检查深度图或调整 --box-grow" % box)

    # 2) 头像白底抠图
    hh, hw = head.shape[:2]
    f = parse_floats(args.face, 4)
    face_px = (f[0] * hw, f[1] * hh, f[2] * hw, f[3] * hh)
    alpha = head_alpha(head, face_px)
    ys, xs = np.where(alpha > 0.05)
    if len(xs) == 0:
        raise SystemExit("头像抠图失败：整张图被判为背景")
    pad = int(0.06 * max(box[2] - box[0], box[3] - box[1]))
    crop = [max(0, xs.min() - pad), max(0, ys.min() - pad),
            min(hw, xs.max() + pad), min(hh, ys.max() + pad)]
    head_c = head[crop[1]:crop[3], crop[0]:crop[2]]
    alpha_c = alpha[crop[1]:crop[3], crop[0]:crop[2]]

    # 3) 等比缩放（或横向拉伸）+ 预乘 alpha
    tw, th = box[2] - box[0], box[3] - box[1]
    if args.warp_fill:
        size = (tw, th)
    else:
        scale = min(tw / head_c.shape[1], th / head_c.shape[0])
        size = (max(8, int(round(head_c.shape[1] * scale))),
                max(8, int(round(head_c.shape[0] * scale))))
    head_r, alpha_r, premul = resize_premul(head_c, alpha_c, size)

    # 4) 贴到 base 上（alpha 合成）
    ox = box[0] + (tw - size[0]) // 2
    oy = box[1] + (th - size[1]) // 2
    ox = max(0, min(base.shape[1] - size[0], ox))
    oy = max(0, min(base.shape[0] - size[1], oy))
    roi = base[oy:oy + size[1], ox:ox + size[0]].astype(np.float32)
    a3 = alpha_r[..., None].astype(np.float32)
    pasted = roi * (1.0 - a3) + head_r.astype(np.float32) * a3

    # 5) 亮度对齐：把贴图区域均值对齐到 base 同区域均值（限制在 ±15%）
    if args.brightness > 0:
        m = alpha_r > 0.5
        if m.sum() > 100:
            gm_h = cv2.cvtColor(head_r, cv2.COLOR_BGR2GRAY).astype(np.float32)[m].mean()
            gm_b = cv2.cvtColor(base[oy:oy + size[1], ox:ox + size[0]], cv2.COLOR_BGR2GRAY
                                ).astype(np.float32)[m].mean()
            if gm_h > 1e-3:
                gain = float(np.clip(gm_b / gm_h, 0.85, 1.15))
                gain = 1.0 + args.brightness * (gain - 1.0)
                head_r = np.clip(head_r.astype(np.float32) * gain, 0, 255).astype(np.uint8)
                pasted = roi * (1.0 - a3) + head_r.astype(np.float32) * a3

    comp = base.copy()
    comp[oy:oy + size[1], ox:ox + size[0]] = np.clip(pasted, 0, 255).astype(np.uint8)

    # 6) 接触阴影
    comp = contact_shadow(comp, box, f[3], args.shadow)

    # 7) 梯度域融合（Poisson / seamlessClone）
    if args.clone > 0:
        mask = np.zeros(base.shape[:2], np.uint8)
        mask[oy:oy + size[1], ox:ox + size[0]] = (alpha_r * 255).astype(np.uint8)
        mask = cv2.erode(mask, np.ones((3, 3), np.uint8), iterations=1)
        center = (int(ox + size[0] / 2), int(oy + size[1] / 2))
        if mask.sum() > 0:
            try:
                seamless = cv2.seamlessClone(comp, base, mask, center, cv2.NORMAL_CLONE)
                comp = np.clip((1.0 - args.clone) * comp.astype(np.float32)
                               + args.clone * seamless.astype(np.float32), 0, 255).astype(np.uint8)
            except cv2.error as exc:
                print("[compose_head] seamlessClone 跳过：%s" % exc)

    # 8) 颗粒匹配
    region = np.zeros(base.shape[:2], np.uint8)
    region[oy:oy + size[1], ox:ox + size[0]] = 255
    comp = match_grain(comp, args.grain, region)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_path), comp)

    if args.debug_dir:
        d = Path(args.debug_dir)
        d.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(d / "head_alpha.png"), (alpha * 255).astype(np.uint8))
        cv2.imwrite(str(d / "paste_box.png"),
                    cv2.rectangle(base.copy(), (box[0], box[1]), (box[2], box[3]),
                                  (0, 0, 255), 2))

    print("[compose_head] head box=%s  paste size=%dx%d  alpha mean=%.2f"
          % (box, size[0], size[1], float(alpha_r.mean())))
    print("[compose_head] saved -> %s" % out_path)


if __name__ == "__main__":
    main()
