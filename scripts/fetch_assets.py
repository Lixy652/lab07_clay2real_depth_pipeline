#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一键拉取本实验需要的第三方资源（权重 + sd.cpp 预编译 CPU 后端）。

    python scripts/fetch_assets.py                     # 直连
    python scripts/fetch_assets.py --proxy http://127.0.0.1:7897
    python scripts/fetch_assets.py --endpoint https://hf-mirror.com

产物：
    models/sd_turbo-q4_0.gguf               主模型（约 1.72GB，q4_0）
    models/control_depth_sd21-fp16.gguf     SD2.1 深度 ControlNet（约 695MB，fp16）
    vendor/sdcpp-ext/                       sd-cli.exe + ggml/stable-diffusion 动态库

Blender 需要单独安装（本脚本不接管 Blender 安装包），4.5.x LTS 即可，
用环境变量 BLENDER_EXE 指定可执行文件路径。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"
VENDOR = ROOT / "vendor" / "sdcpp-ext"

SDCPP_TAG = "master-1017-be48649"
SDCPP_ZIP = "sd-master-be48649-bin-win-cpu-x64.zip"
SDCPP_URL = ("https://github.com/leejet/stable-diffusion.cpp/releases/download/%s/%s"
             % (SDCPP_TAG, SDCPP_ZIP))

MAIN_REPO = "samoudi/sd-turbo-gguf"
MAIN_FILE = "sd_turbo_q4_0.gguf"
MAIN_OUT = "sd_turbo-q4_0.gguf"

CN_REPO = "thibaud/controlnet-sd21"
CN_FILE = "control_v11p_sd21_depth.safetensors"
CN_OUT = "control_depth_sd21-fp16.gguf"

EXPECT = {MAIN_OUT: 1.70, CN_OUT: 0.65}   # GB，粗略下限，用于完整性检查


def human(n):
    return "%.2f GB" % (n / 1073741824.0)


def download(url, dest: Path, proxy=None, resume=True):
    dest.parent.mkdir(parents=True, exist_ok=True)
    have = dest.stat().st_size if dest.exists() else 0
    headers = {"User-Agent": "Mozilla/5.0 (stable-diffusion.cpp fetch script)"}
    if resume and have:
        headers["Range"] = "bytes=%d-" % have
        print("[fetch] resume %s from %s" % (dest.name, human(have)))
    else:
        have = 0
    req = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener()
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener.addheaders = list(headers.items())
    with opener.open(req, timeout=120) as resp:
        total = resp.headers.get("Content-Length")
        total = int(total) + have if total else None
        mode = "ab" if have else "wb"
        done = have
        with open(dest, mode) as fh:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if total:
                    pct = 100.0 * done / total
                    sys.stdout.write("\r  %6.1f%%  %s / %s" % (pct, human(done), human(total)))
                    sys.stdout.flush()
    sys.stdout.write("\n")
    print("[fetch] %s -> %s (%s)" % (url.split("/")[-1], dest.name, human(dest.stat().st_size)))


def fetch_sdcpp(proxy=None):
    if (VENDOR / "sd-cli.exe").exists():
        print("[fetch] sd-cli.exe 已存在，跳过")
        return
    tmp = ROOT / "downloads" / SDCPP_ZIP
    download(SDCPP_URL, tmp, proxy=proxy, resume=False)
    VENDOR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(tmp) as zf:
        zf.extractall(VENDOR)
    print("[fetch] extracted -> %s" % VENDOR)


def fetch_models(endpoint, proxy=None):
    base = endpoint.rstrip("/")

    main_path = MODELS / MAIN_OUT
    if main_path.exists() and main_path.stat().st_size > EXPECT[MAIN_OUT] * 1073741824:
        print("[fetch] %s 已存在，跳过" % MAIN_OUT)
    else:
        download("%s/%s/resolve/main/%s" % (base, MAIN_REPO, MAIN_FILE), main_path, proxy=proxy)

    cn_path = MODELS / CN_OUT
    if cn_path.exists() and cn_path.stat().st_size > EXPECT[CN_OUT] * 1073741824:
        print("[fetch] %s 已存在，跳过" % CN_OUT)
    else:
        tmp_sf = MODELS / CN_FILE
        download("%s/%s/resolve/main/%s" % (base, CN_REPO, CN_FILE), tmp_sf, proxy=proxy)
        sdcli = VENDOR / "sd-cli.exe"
        if not sdcli.exists():
            raise SystemExit("需要 sd-cli.exe 才能把 ControlNet 转成 GGUF，请先执行 --only-sdcpp")
        print("[fetch] 转换 %s -> %s（fp16）" % (CN_FILE, CN_OUT))
        subprocess.check_call([str(sdcli), "-M", "convert", "-m", str(tmp_sf),
                               "-o", str(cn_path), "--type", "f16"],
                              cwd=str(VENDOR))
        tmp_sf.unlink(missing_ok=True)


def verify():
    ok = True
    for name, low in EXPECT.items():
        p = MODELS / name
        if not p.exists():
            print("[verify] 缺失：%s" % p)
            ok = False
            continue
        size = p.stat().st_size
        flag = "OK " if size > low * 1073741824 else "偏小"
        print("[verify] %s %s (%s)" % (flag, p.name, human(size)))
    if not (VENDOR / "sd-cli.exe").exists():
        print("[verify] 缺失：%s" % (VENDOR / "sd-cli.exe"))
        ok = False
    blender = os.environ.get("BLENDER_EXE") or shutil.which("blender")
    print("[verify] Blender: %s" % (blender or "未找到（请设置 BLENDER_EXE）"))
    return ok


def main():
    ap = argparse.ArgumentParser(description="download sd.cpp CPU build + GGUF weights")
    ap.add_argument("--proxy", default=os.environ.get("HTTPS_PROXY"))
    ap.add_argument("--endpoint", default="https://huggingface.co", help="HF 端点（可用镜像）")
    ap.add_argument("--only-models", action="store_true")
    ap.add_argument("--only-sdcpp", action="store_true")
    args = ap.parse_args()

    if not args.only_models:
        fetch_sdcpp(args.proxy)
    if not args.only_sdcpp:
        fetch_models(args.endpoint, args.proxy)
    verify()


if __name__ == "__main__":
    main()
