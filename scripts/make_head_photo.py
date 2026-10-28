#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成一张“白底正面头像”作为拓展项的 --head 输入。

现实中这一步应该换成自己的照片；本仓库没有个人照片可分发，所以用同一个 sd-turbo
直接 txt2img 一张白底正面照（白底正好配合 compose_head.py 的白底软抠图）。

    python scripts/make_head_photo.py --out assets/portrait.png

输出：assets/portrait.png（512×512，白底，正面，无 ControlNet）
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from run_pipeline import MODEL_SD, ROOT, SDCLI, parse_sdcpp_log, run  # noqa: F401

HEAD_PROMPT = ("portrait photo of a young man, front view, looking at camera, "
               "plain white background, soft studio lighting, sharp focus, natural skin")
HEAD_NEGATIVE = "deformed face, blurry, extra limbs, watermark, text"


def main():
    try:  # 控制台可能是 GBK，避免打印 ² 等字符时崩溃
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="assets/portrait.png")
    ap.add_argument("--res", type=int, default=512)
    ap.add_argument("--steps", type=int, default=4)
    ap.add_argument("--cfg", type=float, default=1.2)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--threads", type=int, default=-1)
    ap.add_argument("--prompt", default=HEAD_PROMPT)
    ap.add_argument("--negative", default=HEAD_NEGATIVE)
    args = ap.parse_args()

    out = (ROOT / args.out).resolve() if not Path(args.out).is_absolute() else Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    cmd = [SDCLI, "-m", str(MODEL_SD),
           "-p", args.prompt, "-n", args.negative,
           "-W", str(args.res), "-H", str(args.res),
           "--steps", str(args.steps), "--cfg-scale", str(args.cfg),
           "--sampling-method", "euler_a",
           "-s", str(args.seed), "-t", str(args.threads),
           "-o", str(out)]
    t0 = time.perf_counter()
    _, text, wall = run(cmd, cwd=SDCLI.parent, log_path=ROOT / "output" / "logs" / "portrait_sdcpp.log")
    info = parse_sdcpp_log(text)
    print("[make_head_photo] saved %s  (sampling %.1fs / vae %.1fs / wall %.1fs)"
          % (out, info.get("sampling_s", 0), info.get("vae_s", 0), time.perf_counter() - t0))


if __name__ == "__main__":
    main()
