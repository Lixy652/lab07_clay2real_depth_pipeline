#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""汇总 results/*.json：生成耗时表、几何一致性（边缘倒角距离）指标与缩放曲线。

    python scripts/analyze_runs.py

产物：
    results/summary.md          人读的表格（报告直接引用）
    results/metrics.json        机读指标
    docs/figures/timing_scaling.png   耗时随像素数/步数的变化
    docs/figures/geometry_scores.png  各运行组的几何一致性得分
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"
OUTPUT = ROOT / "output"
FIGS = ROOT / "docs" / "figures"

ORDER = ["runA", "runB", "runC", "runD", "runE", "runF"]


# --------------------------------------------------------------------------------------
# 几何一致性：以白模边缘为参考，量写实图的边缘偏离（倒角距离）
# --------------------------------------------------------------------------------------
def _object_roi(depth_path: Path, pad: int = 12):
    depth = cv2.imread(str(depth_path), cv2.IMREAD_GRAYSCALE)
    if depth is None:
        return None
    # 深度图里物体是“近处”的亮斑；取亮区的外接框作为分析 ROI，避开背景墙面
    mask = (depth > max(24, int(0.18 * depth.max()))).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    if n <= 1:
        return None
    h, w = depth.shape
    x0, y0 = w, h
    x1 = y1 = 0
    for i in range(1, n):
        x, y, ww, hh, area = stats[i]
        if area < 0.002 * w * h:      # 忽略噪声小连通块
            continue
        x0, y0 = min(x0, x), min(y0, y)
        x1, y1 = max(x1, x + ww), max(y1, y + hh)
    if x1 <= x0:
        return None
    return (max(0, x0 - pad), max(0, y0 - pad),
            min(w, x1 + pad), min(h, y1 + pad))


def chamfer(ref_path: Path, test_path: Path, roi) -> dict:
    """ref=clay（白模几何参考），test=photoreal。

    统计方向是“白模边缘 → 写实图边缘”：对每个白模边缘像素，量它到最近写实图边缘的距离。
    这样量的是“白模的轮廓有没有被写实图接住”，不会被写实图自身的材质纹理（皮革/金属噪点）
    反过来污染指标——反方向（写实图→白模）会把纹理边缘全部算成误差。
    """
    ref = cv2.imread(str(ref_path), cv2.IMREAD_GRAYSCALE)
    test = cv2.imread(str(test_path), cv2.IMREAD_GRAYSCALE)
    if ref is None or test is None:
        return {}
    if roi:
        x0, y0, x1, y1 = roi
        ref, test = ref[y0:y1, x0:x1], test[y0:y1, x0:x1]

    ref_edges = cv2.Canny(cv2.GaussianBlur(ref, (3, 3), 0), 40, 110)
    test_edges = cv2.Canny(cv2.GaussianBlur(test, (3, 3), 0), 40, 110)
    if ref_edges.sum() == 0 or test_edges.sum() == 0:
        return {}

    # 以写实图边缘为源做距离变换，度量白模边缘在多远之内被接住
    dist = cv2.distanceTransform(255 - test_edges, cv2.DIST_L2, 3)
    d = dist[ref_edges > 0]
    return {
        "edge_ref_px": int((ref_edges > 0).sum()),
        "edge_test_px": int((test_edges > 0).sum()),
        "chamfer_mean_px": round(float(d.mean()), 2),
        "chamfer_median_px": round(float(np.median(d)), 2),
        "chamfer_p90_px": round(float(np.percentile(d, 90)), 2),
        "coverage_4px": round(float((d <= 4.0).mean()), 3),
        "roi_px": None if not roi else [int(v) for v in roi],
    }


# --------------------------------------------------------------------------------------
def main():
    try:  # 控制台可能是 GBK，避免打印 ² 等字符时崩溃
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    RUNS = collect_runs()
    if not RUNS:
        raise SystemExit("results/ 里没有 run*.json，请先运行 scripts/run_all.ps1")

    rows = []
    for name in RUNS:
        rec = RUNS[name]
        t = rec.get("timing", {})
        res = int(rec["resolution"])
        steps = int(rec["steps"])
        sampling = t.get("sampling_s")
        per_step = round(sampling / steps, 2) if sampling else None
        per_mpx = round(per_step / (res * res / 1e6), 1) if per_step else None
        geo = {}
        clay = OUTPUT / ("%s_clay.png" % name)
        photo = OUTPUT / ("%s_photoreal.png" % name)
        depth = OUTPUT / ("%s_depth.png" % name)
        if clay.is_file() and photo.is_file():
            geo = chamfer(clay, photo, _object_roi(depth) if depth.is_file() else None)
        rows.append({
            "run": name,
            "res": res,
            "steps": steps,
            "cfg": rec["cfg"],
            "control_strength": rec["control_strength"],
            "sampler": rec.get("sampler_reported", rec.get("sampler")),
            "seed": rec["seed"],
            "mist_range_m": rec.get("mist_range_m"),
            "blender_s": t.get("blender_s"),
            "sampling_s": sampling,
            "vae_s": t.get("vae_s"),
            "sdcpp_wall_s": t.get("sdcpp_wall_s"),
            "total_s": t.get("total_s"),
            "per_step_s": per_step,
            "per_step_per_mpx_s": per_mpx,
            **geo,
        })

    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "metrics.json").write_text(
        json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")

    md = render_markdown(rows)
    (RESULTS / "summary.md").write_text(md, encoding="utf-8")
    print(md)

    try:
        plot(rows)
    except Exception as exc:  # pragma: no cover - 画图失败不影响指标
        print("[analyze] plot skipped: %s" % exc)


def collect_runs():
    runs = {}
    for path in sorted(RESULTS.glob("run*.json")):
        if path.name == "metrics.json":
            continue
        rec = json.loads(path.read_text(encoding="utf-8"))
        runs[rec["name"]] = rec
    return {k: runs[k] for k in ORDER if k in runs} | \
           {k: v for k, v in runs.items() if k not in ORDER}


def render_markdown(rows) -> str:
    head = ("| 运行 | 分辨率 | 步数 | CFG | CN | 采样(s) | 单步(s) | 单步/Mpx(s) | "
            "VAE(s) | 总耗时(s) | 白模边缘→写实图边缘 均值(px) | 中位(px) | ≤4px 覆盖率 |\n")
    head += ("|---|---|---|---|---|---|---|---|---|---|---|---|---|\n")
    body = ""
    for r in rows:
        body += "| {run} | {res}² | {steps} | {cfg} | {control_strength} | {sampling_s} | " \
                "{per_step_s} | {per_step_per_mpx_s} | {vae_s} | {total_s} | " \
                "{chamfer_mean_px} | {chamfer_median_px} | {coverage_4px} |\n".format(**{k: r.get(k, "-") for k in [
                    "run", "res", "steps", "cfg", "control_strength", "sampling_s",
                    "per_step_s", "per_step_per_mpx_s", "vae_s", "total_s",
                    "chamfer_mean_px", "chamfer_median_px", "coverage_4px"]})

    # 单步耗时 ~ k × 像素数：用 512² 组拟合 k，再预测 768²
    k512 = [r["per_step_per_mpx_s"] for r in rows
            if r["res"] == 512 and r.get("per_step_per_mpx_s")]
    extra = "\n## 缩放规律\n\n"
    if k512:
        k = float(np.median(k512))
        extra += "- 512² 组拟合：单步耗时 ≈ %.1f s/Mpx × 像素数（= %.2f s/步 @512²）\n" % (k, k * 0.262144)
        pred768 = k * (768 * 768 / 1e6)
        measured = [r["per_step_s"] for r in rows if r["res"] == 768]
        extra += "- 按此预测 768²：%.2f s/步" % pred768
        if measured:
            m = measured[0]
            extra += "；实测 %.2f s/步，偏差 %+.0f%%（含内存带宽/缓存效应）\n" % (m, 100 * (m - pred768) / pred768)
        else:
            extra += "\n"
    return "# 运行汇总\n\n" + head + body + extra


def plot(rows):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    FIGS.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(1, 2, figsize=(10, 4), dpi=150)

    # 左：单步耗时 vs 像素数（同一分辨率的点会重叠，横向加 jitter + 交替偏移标签）
    pts = [(r["res"] ** 2 / 1e6, r["per_step_s"], r["run"]) for r in rows if r.get("per_step_s")]
    if pts:
        groups = {}
        for mpx, per_step, name in pts:
            groups.setdefault(round(mpx, 4), []).append((per_step, name))
        for mpx, items in groups.items():
            items.sort()
            for i, (per_step, name) in enumerate(items):
                jitter = (i - (len(items) - 1) / 2.0) * 0.004
                ax[0].scatter(mpx + jitter, per_step, s=55, zorder=3)
                dy = 8 if i % 2 == 0 else -14
                ax[0].annotate(name, (mpx + jitter, per_step), textcoords="offset points",
                               xytext=(4, dy), fontsize=7.5)
        xs = np.linspace(0.2, max(p[0] for p in pts) * 1.12, 50)
        k = float(np.median([r["per_step_per_mpx_s"] for r in rows if r.get("per_step_per_mpx_s")]))
        ax[0].plot(xs, k * xs, "--", color="gray", label="t ≈ %.1f s/Mpx × pixels" % k)
    ax[0].set_xlabel("pixels (Mpx)")
    ax[0].set_ylabel("seconds per step")
    ax[0].set_title("per-step cost vs. pixel count")
    ax[0].set_ylim(0, max((p[1] for p in pts), default=10) * 1.18)
    ax[0].set_xlim(0.18, max((p[0] for p in pts), default=0.3) * 1.15)
    ax[0].grid(alpha=0.3)
    ax[0].legend(fontsize=8, loc="upper left")

    # 右：各运行组总耗时分解
    names = [r["run"] for r in rows]
    bottom = np.zeros(len(rows))
    for key, label in (("blender_s", "Blender"), ("sampling_s", "sampling"), ("vae_s", "VAE")):
        vals = np.array([float(r.get(key) or 0.0) for r in rows])
        ax[1].bar(names, vals, bottom=bottom, label=label)
        bottom += vals
    ax[1].set_ylabel("seconds")
    ax[1].set_title("run time breakdown")
    ax[1].legend(fontsize=8)
    ax[1].grid(alpha=0.3, axis="y")

    fig.tight_layout()
    fig.savefig(FIGS / "timing_scaling.png")
    plt.close(fig)

    # 几何一致性
    geo = [(r["run"], r.get("chamfer_mean_px"), r.get("coverage_4px")) for r in rows
           if r.get("chamfer_mean_px")]
    if not geo:
        return
    fig, ax = plt.subplots(1, 2, figsize=(10, 3.6), dpi=150)
    ax[0].bar([g[0] for g in geo], [g[1] for g in geo], color="tab:blue")
    ax[0].set_ylabel("chamfer mean (px)")
    ax[0].set_title("edge deviation from clay model (lower is better)")
    ax[0].grid(alpha=0.3, axis="y")
    ax[1].bar([g[0] for g in geo], [g[2] for g in geo], color="tab:green")
    ax[1].set_ylim(0, 1.0)
    ax[1].set_ylabel("clay edges matched within 4 px")
    ax[1].set_title("geometry coverage (higher is better)")
    ax[1].grid(alpha=0.3, axis="y")
    fig.tight_layout()
    fig.savefig(FIGS / "geometry_scores.png")
    plt.close(fig)


if __name__ == "__main__":
    main()
