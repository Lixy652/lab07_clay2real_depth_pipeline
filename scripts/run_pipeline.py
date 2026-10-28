#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一键流水线：Blender 导出控制图 → stable-diffusion.cpp 纯 CPU 出写实图。

示例：

    python scripts/run_pipeline.py --name test512 --res 512 --steps 4 --cfg 1.2

参数（与实验指南一致）：

    --res 512              输出分辨率，512 或 768
    --steps 4              采样步数（Turbo 模型 4~8）
    --cfg 1.2              CFG 强度（Turbo 模型保持 1.0~1.5）
    --control-strength 0.9 ControlNet 强度，1.0 表示完全覆盖初始信息
    --seed 42              随机种子，固定后可复现
    --threads -1           -1 表示占满所有核心
    --control-image 路径    跳过 Blender，直接使用已有深度图（需绝对路径）

输出：
    output/<name>_clay.png / <name>_depth.png / <name>_normal.png
    output/<name>_photoreal.png
    output/logs/<name>_sdcpp.log       sd-cli 原始日志
    results/<name>.json                参数 + 各阶段耗时（报告数据来源）
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODEL_SD = ROOT / "models" / "sd_turbo-q4_0.gguf"
CONTROLNET = ROOT / "models" / "control_depth_sd21-fp16.gguf"
SDCLI = ROOT / "vendor" / "sdcpp-ext" / "sd-cli.exe"
BLENDER_SCRIPT = ROOT / "blender" / "export_control_maps.py"
SCENE_SCRIPT = ROOT / "blender" / "make_demo_scene.py"
BLEND = ROOT / "assets" / "demo_white_model.blend"
OUTPUT = ROOT / "output"
LOGS = OUTPUT / "logs"
RESULTS = ROOT / "results"

PROMPT = ("photorealistic render, studio soft lighting, PBR material, "
          "preserve original 3d geometry, sharp")
NEGATIVE = "deformed geometry, perspective changed"


# --------------------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------------------
def log(msg: str) -> None:
    print("[pipeline] %s" % msg, flush=True)


def run(cmd, cwd=None, log_path=None, echo=True):
    """执行子进程，完整输出写日志文件，返回 (returncode, 输出文本, 墙钟耗时)。"""
    log("run: %s" % " ".join(str(c) for c in cmd))
    t0 = time.perf_counter()
    proc = subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace")
    wall = time.perf_counter() - t0
    if log_path:
        os.makedirs(os.path.dirname(str(log_path)), exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write("$ %s\n\n" % " ".join(str(c) for c in cmd))
            fh.write(proc.stdout or "")
            fh.write("\n[wall clock] %.2f s\n" % wall)
    if echo and proc.stdout:
        tail = proc.stdout.strip().splitlines()
        for line in tail[-12:]:
            print("    " + line)
    if proc.returncode != 0:
        raise SystemExit("命令失败（exit=%d）：%s" % (proc.returncode, " ".join(str(c) for c in cmd)))
    return proc.returncode, proc.stdout or "", wall


def ensure_rgb(path):
    """sd.cpp 的 control image 需要至少 3 通道。"""
    from PIL import Image

    with Image.open(path) as img:
        if img.mode != "RGB":
            img.convert("RGB").save(path)


def latest(pattern):
    files = glob.glob(str(pattern))
    return Path(max(files, key=os.path.getmtime)) if files else None


def find_blender(cli_value=None):
    for cand in (cli_value, os.environ.get("BLENDER_EXE")):
        if cand and Path(cand).is_file():
            return Path(cand)
    which = shutil.which("blender")
    if which:
        return Path(which)
    raise SystemExit(
        "找不到 Blender。请安装 Blender 4.5 LTS，并把可执行文件路径写进环境变量 "
        "BLENDER_EXE，或用 --blender 指定。"
    )


def sha1_of(path, limit=1 << 20):
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        h.update(fh.read(limit))
    return h.hexdigest()


# --------------------------------------------------------------------------------------
# sd-cli 日志解析
# --------------------------------------------------------------------------------------
_NUM = r"([0-9]+(?:\.[0-9]+)?)"


def parse_sdcpp_log(text: str) -> dict:
    """从 sd-cli 日志里抽取采样 / VAE 解码 / 总耗时等关键数字。"""
    info = {}
    patterns = {
        "sampling_s": [r"sampling completed,?\s*taking\s+" + _NUM + r"\s*s?"],
        "vae_s": [r"decod\w*\b.*?taking\s+" + _NUM + r"\s*s",
                  r"VAE.*?decode.*?taking\s+" + _NUM + r"\s*s"],
        "encode_s": [r"encod\w*\b.*?taking\s+" + _NUM + r"\s*s"],
        "total_reported_s": [r"generate_image completed in\s+" + _NUM + r"\s*s",
                             r"total\s+taking\s+" + _NUM + r"\s*s"],
        "it_per_step_s": [r"([0-9]+(?:\.[0-9]+)?)s/it"],
    }
    for key, pats in patterns.items():
        for pat in pats:
            m = re.search(pat, text, flags=re.IGNORECASE)
            if m:
                info[key] = float(m.group(1))
                break
    m = re.search(r"sampling using\s+(.+?)\s+method", text, flags=re.IGNORECASE)
    if m:
        info["sampler"] = m.group(1)
    m = re.search(r"using\s+(\w+)\s+backend", text, flags=re.IGNORECASE)
    if m:
        info["backend"] = m.group(1)
    return info


# --------------------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------------------
def build_argparser():
    p = argparse.ArgumentParser(description="clay -> photoreal CPU pipeline")
    p.add_argument("--name", default="test512", help="本次运行的名称（输出前缀）")
    p.add_argument("--res", type=int, default=512, choices=[512, 768], help="输出分辨率")
    p.add_argument("--steps", type=int, default=4, help="采样步数")
    p.add_argument("--cfg", type=float, default=1.2, help="CFG 强度")
    p.add_argument("--control-strength", type=float, default=0.9, help="ControlNet 强度")
    p.add_argument("--seed", type=int, default=42, help="随机种子")
    p.add_argument("--threads", type=int, default=-1, help="-1 表示占满所有核心")
    p.add_argument("--control-image", default=None,
                   help="跳过 Blender，直接使用已有深度图（建议绝对路径）")
    p.add_argument("--init-image", default=None, help="img2img 初始图（拓展项：光色统一）")
    p.add_argument("--strength", type=float, default=0.75, help="img2img 去噪强度")
    p.add_argument("--sampler", default="euler_a", help="采样器（euler_a / euler / dpm++2m ...）")
    p.add_argument("--prompt", default=PROMPT)
    p.add_argument("--negative", default=NEGATIVE)
    p.add_argument("--samples", type=int, default=64, help="Blender Cycles 采样数")
    p.add_argument("--blender", default=None, help="Blender 可执行文件路径")
    p.add_argument("--blend", default=str(BLEND), help="白模 .blend 路径")
    p.add_argument("--skip-blender", action="store_true",
                   help="复用 output/<name>_depth.png，不再调用 Blender")
    return p


def main():
    try:  # 控制台可能是 GBK，避免打印 ² 等字符时崩溃
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    args = build_argparser().parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    RESULTS.mkdir(parents=True, exist_ok=True)

    for path in (MODEL_SD, CONTROLNET, SDCLI):
        if not path.exists():
            raise SystemExit("缺少文件：%s\n请先运行 python scripts/fetch_assets.py" % path)

    record = {
        "name": args.name,
        "resolution": args.res,
        "steps": args.steps,
        "cfg": args.cfg,
        "control_strength": args.control_strength,
        "seed": args.seed,
        "threads": args.threads,
        "sampler": args.sampler,
        "prompt": args.prompt,
        "negative": args.negative,
        "init_image": args.init_image,
        "strength": args.strength,
        "model": MODEL_SD.name,
        "model_size_mb": round(MODEL_SD.stat().st_size / 1048576, 1),
        "controlnet": CONTROLNET.name,
        "controlnet_size_mb": round(CONTROLNET.stat().st_size / 1048576, 1),
        "timing": {},
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    depth_path = OUTPUT / ("%s_depth.png" % args.name)

    # ---------------- 1) Blender 导出控制图 ----------------
    if args.control_image:
        src = Path(args.control_image)
        if not src.is_file():
            raise SystemExit("control image 不存在：%s" % src)
        shutil.copyfile(src, depth_path)
        ensure_rgb(depth_path)
        cl = src.with_name(src.stem.replace("_depth", "") + "_clay.png")
        if cl.is_file():
            shutil.copyfile(cl, OUTPUT / ("%s_clay.png" % args.name))
        record["control_source"] = str(src)
        log("跳过 Blender，直接使用深度图 %s" % src)
    elif args.skip_blender:
        if not depth_path.is_file():
            raise SystemExit("--skip-blender 需要先有 %s" % depth_path)
        record["control_source"] = "reuse:%s" % depth_path.name
        log("复用已有深度图 %s" % depth_path)
    else:
        blender = find_blender(args.blender)
        blend = Path(args.blend)
        if not blend.is_file():
            log("白模场景不存在，先生成 %s" % blend)
            _, _, t_scene = run([blender, "-b", "--factory-startup", "-P", SCENE_SCRIPT, "--",
                                 "--out", str(blend), "--res", str(args.res)],
                                log_path=LOGS / "make_scene.log")
            record["timing"]["scene_build_s"] = round(t_scene, 2)

        _, blend_out, t_blender = run(
            [blender, "-b", str(blend), "-P", BLENDER_SCRIPT, "--",
             "--outdir", str(OUTPUT), "--name", args.name,
             "--res", str(args.res), "--samples", str(args.samples)],
            log_path=LOGS / ("%s_blender.log" % args.name),
        )
        record["timing"]["blender_s"] = round(t_blender, 2)
        m = re.search(r"mist range = \[([0-9.]+), ([0-9.]+)\]", blend_out)
        if m:
            record["mist_range_m"] = [float(m.group(1)), float(m.group(2))]

        t0 = time.perf_counter()
        src = latest(OUTPUT / ("%s_depth_*.png" % args.name))
        if src is None:
            raise SystemExit("Blender 未产出深度图，请检查日志 %s" % (LOGS / ("%s_blender.log" % args.name)))
        shutil.copyfile(src, depth_path)
        ensure_rgb(depth_path)
        normal_src = latest(OUTPUT / ("%s_normal_*.png" % args.name))
        if normal_src:
            shutil.copyfile(normal_src, OUTPUT / ("%s_normal.png" % args.name))
        record["timing"]["depth_prep_s"] = round(time.perf_counter() - t0, 2)
        record["control_source"] = str(depth_path.relative_to(ROOT))

    # ---------------- 2) 组装并执行 sd-cli ----------------
    photo_path = OUTPUT / ("%s_photoreal.png" % args.name)
    sd_cmd = [
        SDCLI,
        "-m", str(MODEL_SD),
        "-p", args.prompt,
        "-n", args.negative,
        "--control-net", str(CONTROLNET),
        "--control-image", str(depth_path),
        "--control-strength", str(args.control_strength),
        "-W", str(args.res), "-H", str(args.res),
        "--steps", str(args.steps),
        "--cfg-scale", str(args.cfg),
        "--sampling-method", args.sampler,
        "-s", str(args.seed),
        "-t", str(args.threads),
        "-o", str(photo_path),
    ]
    if args.init_image:
        # sd-cli 的工作目录是 vendor/sdcpp-ext，相对路径会被解析到那里，必须给绝对路径
        sd_cmd += ["-i", str(Path(args.init_image).resolve()), "--strength", str(args.strength)]

    _, sd_out, sd_wall = run(sd_cmd, cwd=SDCLI.parent,
                             log_path=LOGS / ("%s_sdcpp.log" % args.name))
    if not photo_path.is_file():
        raise SystemExit("sd-cli 未产出写实图，请检查日志 %s" % (LOGS / ("%s_sdcpp.log" % args.name)))

    parsed = parse_sdcpp_log(sd_out)
    record["timing"]["sdcpp_wall_s"] = round(sd_wall, 2)
    record["timing"].update({k: v for k, v in parsed.items() if k != "sampler"})
    if "sampler" in parsed:
        record["sampler_reported"] = parsed["sampler"]
    record["timing"]["total_s"] = round(
        record["timing"].get("blender_s", 0.0)
        + record["timing"].get("depth_prep_s", 0.0)
        + sd_wall, 2
    )
    record["output"] = str(photo_path.relative_to(ROOT))
    record["sha1"] = sha1_of(photo_path)

    with open(RESULTS / ("%s.json" % args.name), "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2, ensure_ascii=False)

    t = record["timing"]
    log("done: %s" % record["output"])
    log("timing: blender %ss | sampling %ss | vae %ss | sd-cli wall %ss"
        % (t.get("blender_s", "-"), t.get("sampling_s", "-"), t.get("vae_s", "-"),
           round(sd_wall, 1)))
    log("params -> results/%s.json" % args.name)


if __name__ == "__main__":
    sys.exit(main())
