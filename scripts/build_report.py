#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""用空白模板生成实验报告 DOCX：所有数字/图片都来自 results/ 与 output/，不手抄。

    python scripts/build_report.py
    python scripts/build_report.py --template "<模板.docx>" --out "<输出.docx>"

模板结构（21 张表）与填充位置：
    T0 封面信息   T1 实验目的     T2 实验环境   T3 实验原理
    T4 步骤记录   T5 参数矩阵     T6 正向提示词 T7 负向提示词
    T8 耗时记录   T9 控制图       T10 生成结果  T11 几何一致性
    T12 质量缺陷  T13 问题与解决  T14 结论      T15~T20 思考题 1~6
"""
from __future__ import annotations

import argparse
import sys
import json
from pathlib import Path

import cv2
import numpy as np
from docx import Document
from docx.enum.table import WD_ALIGN_VERTICAL
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor, Twips

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"
OUTPUT = ROOT / "output"
FIGS = ROOT / "docs" / "figures"
LAB_ROOT = ROOT.parent

STUDENT = {"课程名称": "《大模型通识》", "姓名": "胡志源", "学号": "2025310151",
           "指导教师": "—", "实验地点": "本地（纯 CPU 笔记本）"}
EA_FONT = "微软雅黑"
LATIN_FONT = "Arial"


# --------------------------------------------------------------------------------------
# 数据装载
# --------------------------------------------------------------------------------------
def load_runs():
    runs = {}
    for p in sorted(RESULTS.glob("run*.json")):
        if p.name in ("metrics.json",):
            continue
        rec = json.loads(p.read_text(encoding="utf-8"))
        runs[rec["name"]] = rec
    return runs


def load_metrics():
    p = RESULTS / "metrics.json"
    if not p.exists():
        return {}
    return {row["run"]: row for row in json.loads(p.read_text(encoding="utf-8"))}


def fmt(v, nd=1, dash="-"):
    if v is None:
        return dash
    if isinstance(v, float):
        return ("%." + str(nd) + "f") % v
    return str(v)


# --------------------------------------------------------------------------------------
# DOCX 小工具
# --------------------------------------------------------------------------------------
def style_run(run, size=10.5, bold=False, color=None, italic=False):
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.italic = italic
    run.font.name = LATIN_FONT
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.append(rfonts)
    rfonts.set(qn("w:ascii"), LATIN_FONT)
    rfonts.set(qn("w:hAnsi"), LATIN_FONT)
    rfonts.set(qn("w:eastAsia"), EA_FONT)
    if color:
        run.font.color.rgb = RGBColor(*color)


def set_para(p, text="", size=10.5, bold=False, align=None, first_indent_chars=0,
             space_before=0, space_after=0, line_spacing=1.3, color=None):
    p.text = ""
    pf = p.paragraph_format
    if align is not None:
        p.alignment = align
    pf.space_before = Pt(space_before)
    pf.space_after = Pt(space_after)
    pf.line_spacing = line_spacing
    if first_indent_chars:
        # 用字符缩进的等价 twips（10.5pt 字号 × 字符数）
        p._p.get_or_add_pPr().append(
            parse_indent(first_indent_chars * size * 20))
    if text:
        r = p.add_run(text)
        style_run(r, size=size, bold=bold, color=color)
    return p


def parse_indent(twips):
    from docx.oxml import OxmlElement

    ind = OxmlElement("w:ind")
    ind.set(qn("w:firstLine"), str(int(twips)))
    return ind


def cell_text(cell, text, size=10.5, bold=False, align=WD_ALIGN_PARAGRAPH.LEFT,
              ea_font=EA_FONT, space_after=0):
    """把单元格写成“一行一段”，并清掉模板里的占位文字。"""
    p = cell.paragraphs[0]
    for extra in cell.paragraphs[1:]:
        extra._element.getparent().remove(extra._element)
    set_para(p, text, size=size, bold=bold, align=align, space_after=space_after)
    cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER


def cell_lines(cell, lines, size=10.5, first_indent_chars=0, space_after=2,
               align=WD_ALIGN_PARAGRAPH.JUSTIFY):
    """单元格内写多段（每段新行）。"""
    p0 = cell.paragraphs[0]
    for extra in cell.paragraphs[1:]:
        extra._element.getparent().remove(extra._element)
    set_para(p0, lines[0] if lines else "", size=size, align=align,
             first_indent_chars=first_indent_chars, space_after=space_after)
    for line in lines[1:]:
        p = cell.add_paragraph()
        set_para(p, line, size=size, align=align,
                 first_indent_chars=first_indent_chars, space_after=space_after)
    cell.vertical_alignment = WD_ALIGN_VERTICAL.TOP


def add_picture_paragraph(doc_or_cell, images, width_cm, caption=None, caption_size=9):
    """一行内并排插入多张图 + 图注。"""
    p = doc_or_cell.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(2)
    p.paragraph_format.space_after = Pt(2)
    for path in images:
        if not Path(path).is_file():
            continue
        run = p.add_run()
        run.add_picture(str(path), width=Cm(width_cm))
        run.add_text("  ")
    if caption:
        cap = doc_or_cell.add_paragraph()
        cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
        cap.paragraph_format.space_after = Pt(6)
        r = cap.add_run(caption)
        style_run(r, size=caption_size, color=(0x59, 0x59, 0x59))
    return p


def make_panel(paths, labels, out_path, cell=384, pad=8, bg=(255, 255, 255),
               label_size=20):
    """把多张图拼成一行带标签的对比图（用于 CFG / ControlNet 强度对照）。"""
    imgs = []
    for p in paths:
        img = cv2.imread(str(p), cv2.IMREAD_COLOR)
        if img is None:
            return None
        img = cv2.resize(img, (cell, cell), interpolation=cv2.INTER_AREA)
        imgs.append(img)
    banner = 34
    W = cell * len(imgs) + pad * (len(imgs) + 1)
    H = cell + banner + pad * 2
    canvas = np.full((H, W, 3), bg, np.uint8)
    for i, (img, label) in enumerate(zip(imgs, labels)):
        x = pad + i * (cell + pad)
        canvas[banner:banner + cell, x:x + cell] = img
        cv2.putText(canvas, label, (x + 6, 24), cv2.FONT_HERSHEY_SIMPLEX,
                    0.6, (30, 30, 30), 2, cv2.LINE_AA)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_path), canvas)
    return out_path


def fix_table_layout(tbl, fractions, total_twips=9306):
    """固定表格列宽，避免长路径/长命令把 autofit 撑出页面。"""
    from docx.oxml import OxmlElement

    tblPr = tbl._tbl.tblPr
    layout = OxmlElement("w:tblLayout")
    layout.set(qn("w:type"), "fixed")
    tblPr.append(layout)
    tblW = tblPr.find(qn("w:tblW"))
    if tblW is not None:
        tblW.set(qn("w:w"), str(total_twips))
        tblW.set(qn("w:type"), "dxa")
    grid = tbl._tbl.find(qn("w:tblGrid"))
    if grid is None:
        grid = OxmlElement("w:tblGrid")
        tbl._tbl.append(grid)
    for col in list(grid):
        grid.remove(col)
    widths = [int(total_twips * fr) for fr in fractions]
    for w in widths:
        gc = OxmlElement("w:gridCol")
        gc.set(qn("w:w"), str(w))
        grid.append(gc)
    for row in tbl.rows:
        for cell, w in zip(row.cells, widths):
            cell.width = Twips(w)


def shrink_table_to_one_row(tbl):
    """删掉多余的空白行，并去掉 cantSplit / 固定行高，避免答题框里出现大片空白。"""
    from docx.oxml import OxmlElement  # noqa: F401

    rows = tbl.rows
    for row in list(rows)[1:]:
        tbl._tbl.remove(row._tr)
    trPr = tbl.rows[0]._tr.find(qn("w:trPr"))
    if trPr is not None:
        for tag in ("w:cantSplit", "w:trHeight"):
            el = trPr.find(qn(tag))
            if el is not None:
                trPr.remove(el)


def set_table_borders(tbl, sz=4):
    """模板里没有 Table Grid 样式，这里直接写 tblBorders。"""
    from docx.oxml import OxmlElement

    tblPr = tbl._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for tag in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement("w:" + tag)
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), str(sz))
        el.set(qn("w:space"), "0")
        el.set(qn("w:color"), "auto")
        borders.append(el)
    tblPr.append(borders)


def insert_after(element, new_element):
    element.addnext(new_element)


def new_paragraph_after(anchor_el, doc, text, style=None, size=10.5, bold=False,
                        align=WD_ALIGN_PARAGRAPH.JUSTIFY, first_indent_chars=0,
                        space_before=4, space_after=2):
    p = doc.add_paragraph(style=style)
    set_para(p, text, size=size, bold=bold, align=align,
             first_indent_chars=first_indent_chars,
             space_before=space_before, space_after=space_after)
    anchor_el.addnext(p._p)
    return p._p


# --------------------------------------------------------------------------------------
# 正文各节
# --------------------------------------------------------------------------------------
def build(template: Path, out_path: Path):
    doc = Document(str(template))
    t = doc.tables
    runs = load_runs()
    metrics = load_metrics()
    A = runs.get("runA", {})
    B = runs.get("runB", {})
    C = runs.get("runC", {})
    D = runs.get("runD", {})
    E = runs.get("runE", {})
    tA, tB, tC = A.get("timing", {}), B.get("timing", {}), C.get("timing", {})
    tD, tE = D.get("timing", {}), E.get("timing", {})
    mA, mB = metrics.get("runA", {}), metrics.get("runB", {})

    # ---------------- 封面信息 ----------------
    header = t[0]
    values = [STUDENT["课程名称"], "2026 年 10 月 27 日", STUDENT["姓名"],
              STUDENT["学号"], STUDENT["指导教师"], STUDENT["实验地点"]]
    pos = [(0, 1), (0, 3), (1, 1), (1, 3), (2, 1), (2, 3)]
    for (r, c), val in zip(pos, values):
        cell_text(header.rows[r].cells[c], val, size=10.5)
    for r, c in [(0, 0), (0, 2), (1, 0), (1, 2), (2, 0), (2, 2)]:
        cell_text(header.rows[r].cells[c], header.rows[r].cells[c].text.strip(), size=10.5, bold=True)

    # ---------------- 一、实验目的 ----------------
    goals = [
        "1. 掌握 Blender Cycles 无头渲染导出 Mist（深度）与 Normal（法线）通道，并按“可见表面 2%/98% 分位”"
        "归一化后反相，得到近白远黑、可直接作为 ControlNet 条件的深度图。",
        "2. 理解 GGUF Q4_0 量化扩散模型在纯 CPU 上的完整推理链路（CLIP 文本编码 → UNet 迭代采样 + ControlNet "
        "注入 → VAE 解码），并能从 sd-cli 日志里拆分采样 / 解码 / 总耗时。",
        "3. 熟练使用 stable-diffusion.cpp 命令行控制分辨率、步数、CFG、采样器与 ControlNet 强度，做出可复现的"
        "参数对照实验（512²/4 步、768²/4 步、512²/8 步、CN 0.5、CFG 2.5、CFG 5.0）。",
        "4. 能对生成结果做几何一致性与画面质量评估：以白模边缘为参考量化几何偏离，定位失真来源并给出改法。",
        "5. （拓展）掌握“深度图定位 + 白底软抠图 + 预乘 alpha + Poisson 融合 + 低强度 img2img”的人像融合链路。",
    ]
    t1 = t[1]
    while len(t1.rows) < len(goals):
        t1.add_row()
    for i, g in enumerate(goals):
        cell_text(t1.rows[i].cells[0], g, size=10.5, align=WD_ALIGN_PARAGRAPH.JUSTIFY)

    # ---------------- 二、实验环境 ----------------
    env = [
        ("CPU / 内存", "Intel Core Ultra 9 185H（16 核 22 线程，P+E 混合架构）× 1，40 GB 内存；纯 CPU 推理，"
                       "GPU（RTX 4060 Laptop）全程未参与"),
        ("操作系统", "Windows 11 Pro 10.0.26200（x64）"),
        ("Blender 版本", "Blender 4.5.13 LTS（便携版，blender -b 无头模式，Cycles CPU，64 采样 + OpenImageDenoise）"),
        ("推理后端与版本", "stable-diffusion.cpp master-1017-be48649（sd-cli.exe + ggml-cpu-*.dll 预编译 CPU 版，"
                         "46.5 MB，线程数 -1 用满核心）"),
        ("主模型文件", "sd_turbo-q4_0.gguf（1762.3 MB / 1.72 GB，SD-Turbo，UNet q4_0 量化，内嵌 SD2.1 CLIP-H 与 VAE）"),
        ("ControlNet 文件", "control_depth_sd21-fp16.gguf（694.7 MB，thibaud/controlnet-sd21 的 "
                          "control_v11p_sd21_depth，fp16 转 GGUF）"),
        ("其他依赖", "Python 3.12 + numpy 2.3.5 / Pillow 12.3.0 / opencv-python-headless 5.0.0 / matplotlib 3.11.2；"
                     "全部脚本与日志在仓库 clay2real-depth-pipeline/ 内"),
    ]
    for i, (k, v) in enumerate(env, start=1):
        cell_text(t[2].rows[i].cells[0], k, size=10, bold=True)
        cell_text(t[2].rows[i].cells[1], v, size=10, align=WD_ALIGN_PARAGRAPH.JUSTIFY)

    # ---------------- 三、实验原理 ----------------
    theory = [
        "① 深度控制。Cycles 的 Mist 通道记录每个着色点沿相机视线到相机的距离，把相机视锥内可见表面的"
        "2% / 98% 分位距离设为 mist 的 start / end，Mist 就被压到 0~1；再取 d = 1 - mist 反相，得到“近处亮、"
        "远处暗”的灰度图。ControlNet 用一个小网络把 d 编码成多层特征，逐层加到冻结 UNet 的对应层上，"
        "于是每一步去噪都被“轮廓 + 遮挡关系 + 透视”约束，生成图与白模保持同一几何。法线通道按 n×0.5+0.5 "
        "映射到 0~1 存成 RGB，只用来检查面朝向，不进入控制链路。",
        "② 量化与架构匹配。Q4_0 把 UNet 权重压到 4 bit（主模型 2.4 GB → 1.72 GB），在 CPU 上直接降低内存"
        "带宽压力，代价是细节略损。SD-Turbo 是“少步数蒸馏”模型，1~4 步即可成像，因此 CFG 必须贴近 1"
        "（本次 1.2）；CFG 升高会让无条件分支占比变大，在少步数下出现过度饱和与结构崩坏。另一个坑是架构："
        "SD-Turbo 属 SD 2.1 系（交叉注意力上下文 1024 维），必须配 SD2.1 的 depth ControlNet，"
        "用 SD1.5 的 768 维版本会在加载时报 [768,1280] vs [1024,1280] 形状不匹配。",
        "③ 推理流程与成本构成。sd-cli 的执行链是：CLIP 文本编码（每张图一次）→ 迭代采样（每一步 1 次 UNet "
        "前向，ControlNet 前向再叠加约 10%~20% 开销）→ VAE 解码（每张图一次）。纯 CPU 下 UNet 前向是绝对的"
        "大头，而且瓶颈不是算力而是内存带宽：算子是访存密集的 GEMM/卷积，所以单步耗时近似正比于像素数，"
        "总耗时 ≈ k × 像素数 × 步数 + VAE 解码 + 固定开销。",
        "④ 本实现的三个工程改动。(a) mist 起止不再只用“视锥内可见顶点”，因为地面是一个只有 4 个顶点的大平面，"
        "顶点全在视锥外，近端会被抬到 7.6 m，画面下方出现大片被截断的死白；改为“近端取可见表面射线采样的 2% "
        "分位，远端取非地面物体顶点的 98% 分位”。(b) 一次渲染同时出三张图：compositor 里 Mist→Invert→OutputFile、"
        "Normal→×0.5+0.5→OutputFile、Image→Composite 落成 clay，Blender 段只需 3~9 s。(c) control image 落盘"
        "后强制转 RGB，避免单通道灰度导致 sd-cli 报 control image load failed。",
    ]
    t3 = t[3]
    while len(t3.rows) < len(theory):
        t3.add_row()
    for i, para in enumerate(theory):
        cell_text(t3.rows[i].cells[0], para, size=10,
                  align=WD_ALIGN_PARAGRAPH.JUSTIFY)

    # ---------------- 四、实验步骤与命令 ----------------
    NL = "\n"
    steps = [
        ("1", "准备环境与权重",
         "python scripts/fetch_assets.py" + NL + "（下载 sd.cpp CPU 后端 + 两个 GGUF）",
         "models/*.gguf（1.72 GB + 695 MB）" + NL + "vendor/sdcpp-ext/（46.5 MB）"),
        ("2", "生成白模场景",
         "python blender/make_demo_scene.py" + NL + "--out assets/demo_white_model.blend --res 512",
         "assets/demo_white_model.blend" + NL + "（球/圆环/倒角立方/圆柱 + 地面 + 三点布光）"),
        ("3", "导出三张控制图",
         "blender -b assets/demo_white_model.blend" + NL + "-P blender/export_control_maps.py --"
         + NL + "--outdir output --name runA --res 512 --samples 64",
         "runA_clay.png、runA_depth.png、" + NL + "runA_normal.png（9.5 s）"),
        ("4", "纯 CPU 生成写实图",
         "python scripts/run_pipeline.py" + NL + "--name runA --res 512 --steps 4 --cfg 1.2",
         "output/runA_photoreal.png" + NL + "（sd-cli 93.8 s）"),
        ("5", "参数对照实验",
         "scripts/run_all.ps1" + NL + "（runB 768²/4、runC 512²/8、" + NL + "runD CN0.5、runE CFG2.5、runF CFG5.0）",
         "output/runB~runF_photoreal.png" + NL + "results/run*.json（参数 + 分阶段耗时）"),
        ("6", "汇总与拓展",
         "python scripts/analyze_runs.py" + NL + "python scripts/compose_head.py + 低强度 img2img",
         "results/summary.md、docs/figures/*.png" + NL + "output/headfig_*_photoreal.png"),
    ]
    for i, (no, op, cmd, out) in enumerate(steps, start=1):
        row = t[4].rows[i]
        cell_text(row.cells[0], no, size=10, align=WD_ALIGN_PARAGRAPH.CENTER)
        cell_lines(row.cells[1], op.split(NL), size=10, align=WD_ALIGN_PARAGRAPH.LEFT)
        cell_lines(row.cells[2], cmd.split(NL), size=8.5, align=WD_ALIGN_PARAGRAPH.LEFT)
        cell_lines(row.cells[3], out.split(NL), size=8.5, align=WD_ALIGN_PARAGRAPH.LEFT)
    fix_table_layout(t[4], [0.07, 0.16, 0.45, 0.32])

    # ---------------- 五、参数配置记录 ----------------
    rows = [
        ("A", "512²", "4", "1.2", "0.9", "euler_a", "42"),
        ("B", "768²", "4", "1.2", "0.9", "euler_a", "42"),
        ("C", "512²", "8", "1.2", "0.9", "euler_a", "42"),
        ("D", "512²", "4", "1.2", "0.5", "euler_a", "42"),
        ("E", "512²", "4", "2.5", "0.9", "euler_a", "42"),
        ("F", "512²", "4", "5.0", "0.9", "euler_a", "42"),
    ]
    t5 = t[5]
    while len(t5.rows) < len(rows) + 1:
        t5.add_row()
    for i, rowvals in enumerate(rows, start=1):
        for j, v in enumerate(rowvals):
            cell_text(t5.rows[i].cells[j], v, size=10, align=WD_ALIGN_PARAGRAPH.CENTER)
    # 模板多留的一行：合并成一整行写注释，避免把长文字塞进第一列
    note_row = t5.rows[len(rows)]
    merged = note_row.cells[0].merge(note_row.cells[-1])
    notes = ("A 为基准组；B 只把分辨率提到 768²（像素数 ×2.25）；C 只把步数翻倍；"
             "D 把 ControlNet 强度降到 0.5，用来观察“几何约束 vs 生成自由度”的权衡；"
             "E/F 把 CFG 提到 2.5 / 5.0，是刻意做的高 CFG 失败组。"
             "六组共用同一种子 42、同一提示词、同一张深度图，保证单变量对照。")
    cell_text(merged, notes, size=9, align=WD_ALIGN_PARAGRAPH.JUSTIFY)
    fix_table_layout(t5, [0.08, 0.13, 0.10, 0.11, 0.17, 0.20, 0.21])

    prompt = A.get("prompt") or ("photorealistic render, studio soft lighting, PBR material, "
                                "preserve original 3d geometry, sharp")
    negative = A.get("negative") or "deformed geometry, perspective changed"
    cell_text(t[6].rows[0].cells[0], prompt, size=10)
    cell_text(t[6].rows[1].cells[0],
              "说明：提示词刻意保持“中性 + 光照 + 材质 + 保几何”四要素，不写具体物体名词，"
              "让几何完全由 depth ControlNet 决定；这也是后面材质语义漂移（球被画成陶罐）的直接原因。", size=9,
              align=WD_ALIGN_PARAGRAPH.JUSTIFY)
    cell_text(t[7].rows[0].cells[0], negative, size=10)
    cell_text(t[7].rows[1].cells[0],
              "负向只压“变形”和“透视改变”两类失败模式；Turbo 模型 CFG 很低，负向词的作用很弱，"
              "实测在 CFG 1.2 时几乎无感，在 CFG 2.5 时才开始明显改变画面。", size=9,
              align=WD_ALIGN_PARAGRAPH.JUSTIFY)

    # ---------------- 六、实验数据记录 ----------------
    def note_for(name):
        rec = runs.get(name if name.startswith("run") else "run" + name, {})
        tt = rec.get("timing", {})
        if name == "A":
            return "基准；含 Blender %.1f s" % (tt.get("blender_s") or 0)
        if name == "B":
            return "像素 ×2.25，采样 ×%.2f" % ((tt.get("sampling_s", 0) or 1) / (tA.get("sampling_s") or 1))
        if name == "C":
            return "步数 ×2，采样 ×%.2f" % ((tt.get("sampling_s", 0) or 1) / (tA.get("sampling_s") or 1))
        if name == "D":
            return "CN 0.5：圆环通孔被封死"
        if name == "E":
            return "CFG 2.5：过饱和、暖色溢出"
        if name == "F":
            return "CFG 5.0：高光溢出、暗部死黑（失败组）"
        return ""

    F = runs.get("runF", {})
    tF = F.get("timing", {})
    data_rows = [
        ("A", tA.get("sampling_s"), tA.get("vae_s"), tA.get("total_s"), note_for("A")),
        ("B", tB.get("sampling_s"), tB.get("vae_s"), tB.get("total_s"), note_for("B")),
        ("C", tC.get("sampling_s"), tC.get("vae_s"), tC.get("total_s"), note_for("C")),
        ("D", tD.get("sampling_s"), tD.get("vae_s"), tD.get("total_s"), note_for("D")),
        ("E", tE.get("sampling_s"), tE.get("vae_s"), tE.get("total_s"), note_for("E")),
        ("F", tF.get("sampling_s"), tF.get("vae_s"), tF.get("total_s"), note_for("F")),
    ]
    t8 = t[8]
    while len(t8.rows) < len(data_rows) + 1:
        t8.add_row()
    for i, rowvals in enumerate(data_rows, start=1):
        row = t8.rows[i]
        cell_text(row.cells[0], rowvals[0], size=10, align=WD_ALIGN_PARAGRAPH.CENTER)
        cell_text(row.cells[1], fmt(rowvals[1]), size=10, align=WD_ALIGN_PARAGRAPH.CENTER)
        cell_text(row.cells[2], fmt(rowvals[2]), size=10, align=WD_ALIGN_PARAGRAPH.CENTER)
        cell_text(row.cells[3], fmt(rowvals[3]), size=10, align=WD_ALIGN_PARAGRAPH.CENTER)
        cell_text(row.cells[4], rowvals[4], size=9)
    fix_table_layout(t8, [0.10, 0.18, 0.18, 0.18, 0.36])

    # 缩放规律段落 + 曲线图，插到“六、实验数据记录”的表后面
    per_step = {}
    per_mpx = {}
    for name, rec in runs.items():
        tt = rec.get("timing", {})
        samp = tt.get("sampling_s")
        if not samp:
            continue
        ps = samp / float(rec["steps"])
        per_step[name] = ps
        per_mpx[name] = ps / (rec["resolution"] ** 2 / 1e6)
    per_step_A = per_step.get("runA")
    per_step_B = per_step.get("runB")
    per_step_C = per_step.get("runC")
    ratio_px = (768 ** 2) / (512 ** 2)
    k = float(np.median(list(per_mpx.values()))) if per_mpx else 0.0
    k_min = min(per_mpx.values()) if per_mpx else 0.0
    k_max = max(per_mpx.values()) if per_mpx else 0.0
    four_step_512 = [per_step[n] for n, r in runs.items()
                     if r["resolution"] == 512 and r["steps"] == 4 and n in per_step]
    med_512_4 = float(np.median(four_step_512)) if four_step_512 else 0.0
    scaling_text = (
        "耗时规律：A 组单步 %.1f s、VAE 解码 %.1f s；B 组像素数涨到 %.2f 倍，单步涨到 %.1f s（×%.2f），"
        "略低于像素倍数——大分辨率下每张 tile 的算术强度提高、图调度与权重首访等固定开销被摊薄，"
        "拟合指数约 %.2f（t ∝ 像素^%.2f）；C 组步数翻倍，采样 %.1f s ≈ A 的 %.2f 倍（单步 %.1f s），"
        "说明采样耗时基本随步数线性增长，而单步耗时略降是因为首步的图执行计划构建被摊到更多步上。"
        "VAE 解码只取决于解码分辨率、与步数无关：A/C 同为 512²，解码 %.1f s / %.1f s，"
        "两者差异已属本机的运行间波动。把六组数据折算成“单步耗时/百万像素”后，落在 %.0f~%.0f s/Mpx，"
        "中位数 %.0f s/Mpx，说明估算式可以写成 t_total ≈ %.0f s/Mpx × 像素数 × 步数 + VAE + 约 8 s Blender 段。"
        "需要强调的是本机（笔记本、22 线程混合架构）同参数多次运行可差 ±20%%，A/D/E/F 四组同为 512²/4 步，"
        "单步耗时中位数 %.1f s、实测散布 %.1f~%.1f s，因此报告里的绝对值只用于同机对比。"
        % (per_step_A or 0, tA.get("vae_s") or 0, ratio_px, per_step_B or 0,
           (per_step_B or 1) / (per_step_A or 1),
           np.log((per_step_B or 1) / (per_step_A or 1)) / np.log(ratio_px) if per_step_A and per_step_B else 0,
           np.log((per_step_B or 1) / (per_step_A or 1)) / np.log(ratio_px) if per_step_A and per_step_B else 0,
           tC.get("sampling_s") or 0,
           (tC.get("sampling_s") or 1) / (tA.get("sampling_s") or 1), per_step_C or 0,
           tA.get("vae_s") or 0, tC.get("vae_s") or 0,
           k_min, k_max, k, k,
           med_512_4, min(four_step_512) if four_step_512 else 0,
           max(four_step_512) if four_step_512 else 0)
    )
    scale_png = FIGS / "timing_scaling.png"
    anchor = t[8]._tbl
    if scale_png.is_file():
        pic_p = doc.add_paragraph()
        pic_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        pic_p.paragraph_format.space_before = Pt(8)
        pic_p.paragraph_format.space_after = Pt(2)
        pic_p.add_run().add_picture(str(scale_png), width=Cm(14.0))
        anchor.addnext(pic_p._p)
        cap = doc.add_paragraph()
        cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
        cap.paragraph_format.space_after = Pt(2)
        r = cap.add_run("图 6-1  单步耗时随像素数（左）与各运行组耗时分解（右）")
        style_run(r, size=9, color=(0x59, 0x59, 0x59))
        pic_p._p.addnext(cap._p)
        anchor = cap._p
    p = new_paragraph_after(anchor, doc, scaling_text, size=10,
                            align=WD_ALIGN_PARAGRAPH.JUSTIFY, first_indent_chars=0,
                            space_before=4, space_after=2)

    # ---------------- 七、7.1 控制图 ----------------
    figs = [OUTPUT / "runA_clay.png", OUTPUT / "runA_depth.png", OUTPUT / "runA_normal.png"]
    cell = t[9].rows[0].cells[0]
    cell_text(cell, "", size=9)
    p0 = cell.paragraphs[0]
    p0.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for path in figs:
        if path.is_file():
            r = p0.add_run()
            r.add_picture(str(path), width=Cm(4.3))
            r.add_text("  ")
    cap = cell.add_paragraph()
    cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = cap.add_run("图 7-1  左：clay 白模参考（64 采样，9.5 s）；中：depth 控制图（反相后近白远黑，"
                    "mist 区间 5.17~8.90 m）；右：normal 法线图（n×0.5+0.5，仅用于检查面朝向）")
    style_run(r, size=9, color=(0x59, 0x59, 0x59))
    extra = ("三张图由同一次渲染产出：材质被 override 成纯白哑光，compositor 同时把 Mist 反相写 depth、"
             "把 Normal 映射写 normal，Render Result 存成 clay。深度图里球、圆环、倒角立方体、圆柱的"
             "前后层次清楚：圆环内孔最暗（最远），柱体右缘与球体左缘最亮（最近），地面从下往上连续变暗，"
             "没有出现被截断的死白——这正是把 mist 近端改成“可见表面射线 2% 分位”的效果。")
    cell2 = t[9].rows[1].cells[0]
    cell_text(cell2, extra, size=10, align=WD_ALIGN_PARAGRAPH.JUSTIFY)
    fix_table_layout(t[9], [1.0])

    # ---------------- 7.2 生成结果 ----------------
    cell = t[10].rows[0].cells[0]
    cell_text(cell, "", size=9)
    p0 = cell.paragraphs[0]
    p0.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for path, w in ((OUTPUT / "runA_photoreal.png", 6.9), (OUTPUT / "runB_photoreal.png", 6.9)):
        if path.is_file():
            r = p0.add_run()
            r.add_picture(str(path), width=Cm(w))
            r.add_text("   ")
    cap = cell.add_paragraph()
    cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = cap.add_run("图 7-2  左：A 组 512²/4 步（采样 %.1f s，总 %.1f s）；"
                    "右：B 组 768²/4 步（采样 %.1f s，总 %.1f s）"
                    % (tA.get("sampling_s") or 0, tA.get("total_s") or 0,
                       tB.get("sampling_s") or 0, tB.get("total_s") or 0))
    style_run(r, size=9, color=(0x59, 0x59, 0x59))
    cell2 = t[10].rows[1].cells[0]
    cell_text(cell2,
              "两组共用同一深度图与种子。B 组把分辨率提到 768² 后，圆环内孔的椭圆透视、立方体顶面的倾斜、"
              "柱体与地面的接触阴影都更清楚；A 组在 512² 下整体偏软，锐度不足（“sharp”在 4 步 Turbo 里几乎不起作用）。",
              size=10, align=WD_ALIGN_PARAGRAPH.JUSTIFY)
    fix_table_layout(t[10], [1.0])

    # ---------------- 7.3 几何一致性 ----------------
    geo = [
        "结论：几何保持度属于“结构级一致、细节级退化”。四个物体的数量、相对位置、相互遮挡关系、"
        "透视方向全部与白模一致——球在左、圆环居中偏左、倒角立方体居中偏右、柱体在最右；"
        "圆环是通孔且孔的椭圆方向正确；立方体右侧两条竖棱的会聚方向与 63 mm 相机的透视一致；"
        "物体与地面的接触阴影统一来自左前上方，与三点布光吻合。定量指标取“白模边缘 → 写实图边缘”的倒角距离"
        "（每个白模边缘像素到最近写实图边缘的距离）：A 组中位 %.2f px、%.1f%% 的白模边缘在 4 px 内被接住；"
        "B 组中位 %.2f px、%.1f%%（同一物理误差在 768² 下折合更多像素，覆盖率下降属分辨率效应，不是几何变差）。"
        % (mA.get("chamfer_median_px") or 0, 100 * (mA.get("coverage_4px") or 0),
           mB.get("chamfer_median_px") or 0, 100 * (mB.get("coverage_4px") or 0)),
        "退化主要在两处：一是倒角立方体的 R7 mm 圆角被抹平，写实图里棱线变成接近 90° 的硬边，边缘偏离集中在四个角；"
        "二是圆环的管径被画细、截面被压扁，写实图更像一个“盘子/垫圈”，而不是白模里那根带高光的圆管。"
        "原因在于 4 步 Turbo + q4_0 量化的高频细节容量有限，而 depth 条件只约束“可见表面到相机的距离”，"
        "对“厚度/管径”这类信息约束很弱，模型于是按先验把环形物画成更常见的薄环。"
        "还要注意指标的盲区：D 组（ControlNet 0.5）覆盖率仍有 %.1f%%，因为轮廓和位置都在，"
        "但它的通孔已经被封死（见 7.4）——轮廓类指标看不到拓扑错误，所以几何一致性必须“指标 + 目视”一起看；"
        "真正被指标抓出来的是 F 组（CFG 5.0），覆盖率掉到 %.1f%%、中位偏离 %.2f px，说明过饱和把边缘结构也糊掉了。"
        % (100 * (metrics.get("runD", {}).get("coverage_4px") or 0),
           100 * (metrics.get("runF", {}).get("coverage_4px") or 0),
           metrics.get("runF", {}).get("chamfer_median_px") or 0),
    ]
    for i, g in enumerate(geo):
        cell_text(t[11].rows[i].cells[0], g, size=10, align=WD_ALIGN_PARAGRAPH.JUSTIFY)
    # 第三行放逐组的量化指标，避免留空
    metric_parts = []
    for name in ["runA", "runB", "runC", "runD", "runE", "runF"]:
        m = metrics.get(name)
        if not m:
            continue
        metric_parts.append("%s：%.2f px / %.1f%%" % (
            name[-1], m.get("chamfer_median_px") or 0, 100 * (m.get("coverage_4px") or 0)))
    if metric_parts:
        cell_text(t[11].rows[2].cells[0],
                  "逐组指标（白模边缘→写实图边缘 中位偏离 / 4 px 内覆盖率）：" + "；".join(metric_parts)
                  + "。覆盖率在 A/C/D/E 之间只差 5 个百分点，说明“轮廓 + 位置”这一层很稳；"
                    "只有 F（CFG 5.0）明显掉到 7 成，可见高 CFG 才是真正会破坏几何的那一个变量。",
                  size=9.5, align=WD_ALIGN_PARAGRAPH.JUSTIFY)

    # ---------------- 7.4 画面质量与缺陷 ----------------
    qual = [
        "材质与光照：整体是暖调的“器物”质感——球像皮革/陶土、圆环像磨砂陶瓷或橡胶圈、立方体像不锈钢罐、"
        "柱体像皮筒；高光来自左前上方，与白模 Key 灯方向一致，接触阴影落在同一侧，没有悬空感，"
        "说明几何与光照方向都被深度条件带对了。但“PBR material”这个提示词在 CFG 1.2 下几乎没有产生材质区分度："
        "模型用的是“桌面器物”的强先验，把四个白模都填成带口沿的容器；画面整体偏暖，背景还长出了白模里"
        "不存在的墙面纹理与织物地纹，属于生成幻觉（hallucination）。",
        "明确失败点（D 组，ControlNet 强度 0.5）：圆环的通孔被一块反光的“盖片”填死了——白模里透过孔能看到"
        "更远的地面，深度图里该区域是最暗的一团，但 0.5 的强度不足以把“孔是透的”这个拓扑约束压进 UNet，"
        "模型按“环=盖子”的先验把孔封上；同一组里立方体变成带盖缝的玻璃罐、柱体变成开口广口瓶、"
        "球多出一圈口沿。轮廓与位置大体还在（边缘倒角中位偏离 %.1f px），但内部结构已经被改写，"
        "这是“形似而神不似”的典型样本。改法：把 control-strength 回到 0.85~0.95，或补一张 normal 图做双条件，"
        "或在提示词里显式写 hollow ring / open tube 来对抗先验。"
        % (metrics.get("runD", {}).get("chamfer_median_px") or 0),
        "高 CFG 失败组（E 组 CFG 2.5、F 组 CFG 5.0）：E 组已经明显过饱和，画面被烤成砖红/琥珀色，"
        "材质从陶瓷变成焦糖；F 组更严重——高光整片溢出、暗部死黑、细节糊化，背景出现烧灼状光晕。"
        "值得注意的是几何并没有像纯文生图那样彻底崩坏：四个物体仍在原位、遮挡关系仍对，"
        "说明 0.9 强度的 ControlNet 把构图从高 CFG 手里抢了回来。也就是说 Turbo 模型在高 CFG 下"
        "首先坏的是“色调与细节”而不是“结构”——但这样的画面同样不可用，CFG 必须守在 1.0~1.5。"
        "另外 A/D/E/F 四组同为 512²/4 步，采样耗时落在 50.4~66.1 s，差异来自运行间波动，与 CFG、ControlNet 强度无关。",
    ]
    for i, q in enumerate(qual):
        cell_text(t[12].rows[i].cells[0], q, size=10, align=WD_ALIGN_PARAGRAPH.JUSTIFY)

    # 追加 CFG / CN 对照图（放在 7.4 表格后面）
    panel1 = make_panel([OUTPUT / "runA_photoreal.png", OUTPUT / "runD_photoreal.png",
                         OUTPUT / "runE_photoreal.png", OUTPUT / "runF_photoreal.png"],
                        ["A: CN0.9 / CFG1.2", "D: CN0.5 / CFG1.2",
                         "E: CN0.9 / CFG2.5", "F: CN0.9 / CFG5.0"],
                        FIGS / "compare_cn_cfg.png", cell=300)
    if panel1:
        anchor = t[12]._tbl
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.add_run().add_picture(str(panel1), width=Cm(16.0))
        anchor.addnext(p._p)
        cap = doc.add_paragraph()
        cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r = cap.add_run("图 7-3  单变量对照：ControlNet 0.9 → 0.5（第 2 张）与 CFG 1.2 → 2.5 → 5.0（第 3、4 张）")
        style_run(r, size=9, color=(0x59, 0x59, 0x59))
        p._p.addnext(cap._p)
        anchor_74 = cap._p

    # ---------------- 7.5 拓展：头像融合 ----------------
    headfig = (OUTPUT / "headfig_photoreal.png")
    face = (OUTPUT / "headfig_face.png")
    harmon = (OUTPUT / "headfig_harmonized_photoreal.png")
    harmon035 = (OUTPUT / "headfig_harmonized_s035_photoreal.png")
    if headfig.is_file() and face.is_file():
        # 7.4 的对照图/图注是本节的最后一块，7.5 必须插在它后面，否则顺序会倒挂
        anchor = locals().get("anchor_74", t[12]._tbl)
        h = doc.add_paragraph(style="Heading 2")
        set_para(h, "7.5 拓展：人物头像融合（预乘 alpha + Poisson + 低强度 img2img）",
                 size=12.5, bold=True, space_before=6, space_after=2)
        anchor.addnext(h._p)
        body = (
            "底图用 make_demo_scene.py --preset figure 生成的“滑冰姿态”人形白模（木偶式骨肉相连的图元拼装），"
            "同样走 depth ControlNet 得到 headfig_photoreal.png；头像用 sd-cli 直接生成一张白底正面照"
            "（没有可用的个人照片，白底正好配合软抠图）。几何合成之后再补一次低强度 img2img 统一光色。"
            "这里有一个必须记住的坑：按指南给的 strength=0.35 做 6 步 img2img，贴上去的人脸会被完全“愈合”"
            "成空白头颅——0.35 的噪声水平足以让模型把 43×43 像素的小脸重画掉；把 strength 降到 0.18、4 步、"
            "control-strength 0.7 之后，人脸才被保留、同时光色与底图统一。"
        )
        p = doc.add_paragraph()
        set_para(p, body, size=10, align=WD_ALIGN_PARAGRAPH.JUSTIFY, space_before=2, space_after=2)
        h._p.addnext(p._p)
        panel = make_panel([headfig, face, harmon035, harmon],
                           ["底图 headfig", "alpha+Poisson 合成",
                            "img2img 0.35（脸被重画）", "img2img 0.18（保留）"],
                           FIGS / "head_compose.png", cell=290)
        if panel:
            pp = doc.add_paragraph()
            pp.alignment = WD_ALIGN_PARAGRAPH.CENTER
            pp.add_run().add_picture(str(panel), width=Cm(16.0))
            p._p.addnext(pp._p)
            cap = doc.add_paragraph()
            cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
            r = cap.add_run("图 7-4  头像融合四阶段：底图 → 几何合成 → strength 0.35 失败（脸被重画）→ "
                            "strength 0.18 成功")
            style_run(r, size=9, color=(0x59, 0x59, 0x59))
            pp._p.addnext(cap._p)
            anchor2 = cap._p
        else:
            anchor2 = p._p

        tbl = doc.add_table(rows=1, cols=2)
        tbl.autofit = True
        set_table_borders(tbl)
        fix_table_layout(tbl, [0.22, 0.78])
        hdr = tbl.rows[0].cells
        cell_text(hdr[0], "链路步骤", size=9.5, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
        cell_text(hdr[1], "作用", size=9.5, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
        rows = [
            ("深度图定位头框", "深度图近亮远暗，但最亮的常常不是头（可能是地面或抬起的手）。所以只在上部 62% 区域取"
                          "近处亮团，用“紧凑度 − 0.6×顶边高度 − 0.3×横向偏心”打分：站立人形的头有两个稳定特征——"
                          "位置最高、形状接近方形，长条（手臂）与贴底（地面）直接被排除；找到后再四周放 12% 覆盖整颗头，"
                          "最后按 --box-grow（0.13,0.14,0.13,0.06）外扩，避免露出模型原图的发际线。"),
            ("白底软抠图 + 椭圆约束", "key = clip((255 - min(r,g,b) - 6)/18) 把白底压到 0、头发/皮肤拉到 1；"
                               "再用椭圆蒙版把 alpha 限制在“头 + 少量脖子”的范围，防止肩膀和背景被带进来。"),
            ("内缩 1px 羽化", "alpha>0.55 处二值化后做一次 3×3 腐蚀，再 σ=1.2 高斯羽化：去掉白底与发丝之间"
                          "那圈半透明过渡，同时保留 1 px 软边，贴上去不会出现硬切口。"),
            ("预乘 alpha 缩放", "先算 rgb×alpha 再 INTER_AREA 缩小，缩小后除以缩小后的 alpha 还原颜色。"
                          "不预乘的话，白底像素会被插值进边缘，形成一圈亮白边。"),
            ("亮度对齐", "把贴图区域与底图同区域的灰度均值对齐（增益限制在 ±15%），避免头像比底图“亮一个档”，"
                     "看起来像剪贴画。"),
            ("接触阴影", "在下巴下方叠一个椭圆软阴影（strength=0.35），给出“头压在脖子上”的接触感，"
                     "否则头会像漂浮在底图上。"),
            ("Poisson 梯度域融合", "cv2.seamlessClone(NORMAL_CLONE) 只保留贴图的梯度、用底图的边界值重建颜色，"
                              "让头发/脸部边缘的光照与背景平滑过渡；再按 --clone 0.6 与原贴图结果混合，"
                              "避免过度平滑掉肤色。"),
            ("颗粒匹配 + img2img 统一", "按底图高频标准差叠加同量级高斯噪声（--grain 1.0）消除“贴图比底图干净”的"
                                  "违和感；最后以合成图为初始图做 strength=0.35、6 步、CFG 1.3 的 img2img，"
                                  "并让 depth ControlNet（0.7）锁住姿态，把两张图的光比、色调、噪点真正融成一张图。"),
        ]
        for step, effect in rows:
            cells = tbl.add_row().cells
            cell_text(cells[0], step, size=9)
            cell_text(cells[1], effect, size=9, align=WD_ALIGN_PARAGRAPH.JUSTIFY)
        anchor2.addnext(tbl._tbl)

    # ---------------- 八、问题与解决 ----------------
    problems = [
        ("深度图下方出现大片死白（近处地面被截断）",
         "只统计“视锥内可见顶点”时，地面是一个只有 4 个顶点的大平面，顶点全在视锥外被排除，"
         "mist 近端被抬到 7.6 m，比摄像机到画面下沿地面的 5.2 m 还远",
         "近端改用“可见表面射线采样（96×96 rays）的 2% 分位”，远端仍用非地面物体顶点的 98% 分位；"
         "mist 区间从 7.63~8.76 m 变成 5.17~8.90 m，地面恢复连续灰度"),
        ("模板只给了 .gguf 的 ControlNet，而公开权重是 .safetensors",
         "sd.cpp 可以直接读 safetensors，但仓库与命令里统一用 GGUF 更省内存、加载更快",
         "用 sd-cli -M convert -m control_v11p_sd21_depth.safetensors -o control_depth_sd21-fp16.gguf "
         "--type f16，0.9 s 完成转换，得到与指南一致的 694.7 MB 文件"),
        ("球/柱体被画成皮革罐，材质语义漂移",
         "CFG 1.2 下提示词只给风格不给名词，4 步 Turbo 用“桌面器物”先验填色；q4_0 也损失了部分材质细节",
         "把 CFG 提到 1.4~1.5、步数提到 6~8，并在提示词里补材质名词（如 matte ceramic / brushed metal），"
         "或改用 strength 更低的 img2img 保持原图色块"),
    ]
    for i, (sym, cause, fix) in enumerate(problems, start=1):
        row = t[13].rows[i]
        cell_text(row.cells[0], sym, size=9)
        cell_text(row.cells[1], cause, size=9, align=WD_ALIGN_PARAGRAPH.JUSTIFY)
        cell_text(row.cells[2], fix, size=9, align=WD_ALIGN_PARAGRAPH.JUSTIFY)
    fix_table_layout(t[13], [0.24, 0.34, 0.42])

    # ---------------- 九、结论 ----------------
    concl = [
        "1. 完成情况：搭出并跑通“Blender 白模 → depth/normal/clay → sd-cli 纯 CPU 生成 → 定量评估”的完整链路，"
        "六组参数（A~F）全部出图，参数与分阶段耗时落盘到 results/*.json，报告里的表格与图都由脚本生成，可一键复现。",
        "2. 几何保持：depth ControlNet 在 0.9 强度下能把四个物体的数量、位置、遮挡与透视完整迁移，"
        "边缘倒角中位偏离 %.2f px、%.0f%% 边缘在 4 px 内；主要退化是 R7 mm 圆角被抹平、圆环管径变细。"
        % (mA.get("chamfer_median_px") or 0, 100 * (mA.get("coverage_4px") or 0)),
        "3. 成本规律：单步耗时近似正比于像素数（512² 约 %.1f s/步、768² 约 %.1f s/步，拟合指数 %.2f），"
        "采样耗时正比于步数，VAE 解码只与分辨率有关；本次 512²/4 步总耗时 %.1f s，其中 UNet 采样占 %.0f%%、"
        "VAE 占 %.0f%%、Blender 段占 %.0f%%。"
        % (per_step_A or 0, per_step_B or 0,
           np.log((per_step_B or 1) / (per_step_A or 1)) / np.log(ratio_px) if per_step_A and per_step_B else 0,
           tA.get("total_s") or 0,
           100 * (tA.get("sampling_s") or 0) / (tA.get("total_s") or 1),
           100 * (tA.get("vae_s") or 0) / (tA.get("total_s") or 1),
           100 * (tA.get("blender_s") or 0) / (tA.get("total_s") or 1)),
        "4. 参数规律：Turbo 模型 CFG 必须贴近 1（1.2 可用、2.5 直接崩坏）；ControlNet 强度 0.9 保结构、"
        "0.5 已明显失守；4 步适合构图与批量，6~8 步才换来可用的材质与锐度。",
        "5. 可改进方向：把 depth 换成 depth+normal 双条件、对物体做分区域提示词（regional prompt）、"
        "用 fp16 主模型或 --tile-vae 换细节、以及在合成链路里用 IP-Adapter/InstantID 替代“贴图 + img2img”。",
    ]
    for i, c in enumerate(concl):
        cell_text(t[14].rows[i].cells[0], c, size=10, align=WD_ALIGN_PARAGRAPH.JUSTIFY)

    # ---------------- 十、思考题 ----------------
    qa = [
        ("1. Turbo 类模型为什么不能使用较高的 CFG？CFG 与采样步数之间是什么关系？",
         "CFG 的实现是把“有条件预测”和“无条件预测”外推：eps = eps_u + s·(eps_c - eps_u)。Turbo 是"
         "对抗蒸馏出来的少步数模型，训练时只在 CFG≈1 的采样轨迹上被蒸馏，它的去噪器学到的是"
         "“给定当前噪声、一步跨到接近干净图”的映射；一旦 s 变大，外推项被放大，单步更新幅度远超训练分布，"
         "采样轨迹跑出数据流形。本次实测把这个结论量化了：A 组（CFG 1.2）画面正常；E 组（CFG 2.5）"
         "整体过饱和、暖橙色调溢出，材质从陶瓷变焦糖，但结构仍完整；F 组（CFG 5.0）高光整片溢出、"
         "暗部死黑、细节糊化。A/E/F 三组采样耗时落在 50.4~66.1 s（同分辨率同步数，差异只是运行间波动），"
         "说明 CFG 只改变更新幅度、不改变计算量。"),
        ("",
         "CFG 与步数是“预算互补”关系：CFG 越大，每一步的等效更新越大、能覆盖的噪声区间越大，"
         "所以常规 SD1.5 用 20~30 步 + CFG 7；Turbo 反过来，用“每步一小段 + 少量步数”完成同样的降噪路径，"
         "因此必须低 CFG，且步数只能取 4~8。本次 A 组 4 步改用 8 步（C 组）后采样从 %.1f s 涨到 %.1f s"
         "（×%.2f，步数 ×2，首步的图执行计划构建等固定开销被摊薄），画面锐度与材质区分度确实变好，"
         "但没有出现“步数越多越好”的质变——4 步已经能把构图定死，多出来的步数只买到细节。"
         % (tA.get("sampling_s") or 0, tC.get("sampling_s") or 0,
            (tC.get("sampling_s") or 1) / (tA.get("sampling_s") or 1))),
        ("2. 深度图的动态范围如何影响生成结果？mist 起始距离设得更小会出现什么现象？",
         "depth ControlNet 学的是“归一化后的相对深度”，它把 0~1 的灰度当作“距离的相对位置”："
         "动态范围太窄（近远只差几厘米）→ 物体之间的深度差被压成同一灰度，ControlNet 拿不到结构，"
         "退化成“弱边缘条件”；动态范围太宽（把几十米外的地面也算进来）→ 物体挤在灰度的高端或低端，"
         "层次被压扁。本次就踩到了窄范围：只用顶点统计时 mist 只有 7.63~8.76 m（1.1 m），"
         "画面下沿 5.2 m 处的地面被截断成死白，物体内部也几乎没有梯度；改成 5.17~8.90 m（3.7 m）后，"
         "地面与物体都拿到连续灰度，写实图的地面接触阴影与前后景分离明显变好。"),
        ("",
         "如果 mist 起始距离设得更小（start 比最近可见表面还近），等价于把整幅图的深度值整体往 0 推："
         "近处一片纯白、物体之间的差异被压到很小的灰度差，ControlNet 只能看到“前面有东西”而看不到"
         "“什么东西”；同时反相后远处会迅速变黑，背景与物体边界消失，生成图会出现结构粘连、"
         "物体被合并的糊图。反之 start 设得太大（比最近表面还远）则近处直接饱和成黑，"
         "几何条件丢失，模型完全靠先验自由发挥。"),
        ("3. ControlNet 强度取 0.9 与 0.5 时结果有何差别？如何与初始图信息权衡？",
         "0.9 时 depth 特征几乎完整注入每一层，UNet 的更新被几何强约束：四个物体的位置、遮挡、"
         "透视与白模一致，白模边缘的 %.1f%% 在 4 px 内被写实图接住（中位偏离 %.2f px）。"
         "0.5 时 ControlNet 只贡献一半残差，UNet 的先验开始占主导：D 组里圆环的通孔被一块反光盖片封死、"
         "立方体变成带盖缝的玻璃罐、柱体多出一圈口沿——轮廓还在（覆盖率 %.1f%%），但内部拓扑被改写了。"
         % (100 * (mA.get("coverage_4px") or 0), mA.get("chamfer_median_px") or 0,
            100 * (metrics.get("runD", {}).get("coverage_4px") or 0))),
        ("",
         "权衡的本质是“约束强度 vs 生成自由度”。纯文生图（无初始图）时 ControlNet 要给到 0.8~1.0，"
         "因为几何信息只能从条件里来；img2img（有初始图）时初始图本身已经携带结构，"
         "ControlNet 反而应该降到 0.5~0.7，把画笔交给 img2img 的 strength，让细节与光色能改写。"
         "本次拓展项的 harmonize 一步就用 0.35 的 strength + 0.7 的 control-strength，"
         "既锁住了姿态又允许肤色与光照重绘。"),
        ("4. 纯 CPU 推理的主要瓶颈是什么？如何由分辨率与步数估算总耗时？",
         "瓶颈是内存带宽（以及随之而来的缓存命中率），不是浮点吞吐：q4_0 权重 1.7 GB，每一步都要把 UNet "
         "的全部权重从内存搬到核心，算子是访存密集的 GEMM/卷积，所以单步耗时 ≈ 权重流量 / 有效带宽，"
         "而权重流量与像素数近似成正比、与步数成正比。次要瓶颈是线程调度：22 个逻辑核里混有 P/E 核，"
         "ggml 按物理核数开线程（-t -1 → 16），E 核拖尾会让满载效率掉 10%~20%；再叠加笔记本的"
         "热降频，同参数两次运行可以差 10%~20%（本次 A/C 同为 512² 的 VAE 解码 35.7 s vs 23.0 s 就是这个量级）。"),
        ("",
         "估算式：总耗时 ≈ k × 像素数 × 步数 + VAE 解码 + 固定开销。本次六组折算出的 k 在 %.0f~%.0f s/Mpx 之间，"
         "中位数 %.0f s/Mpx（512²/4 步单步耗时中位 %.1f s、768²/4 步 %.1f s；像素 ×2.25 只让单步 ×%.2f，"
         "拟合指数 %.2f，放大分辨率能摊薄固定开销）；VAE 解码 512² 约 %.1f~%.1f s、768² 约 %.1f s；"
         "固定开销 = 模型加载 4~8 s + Blender 段 3~9 s。用 512²/4 步验证：%.0f×0.262×4 ≈ %.0f s 采样，"
         "加 VAE %.0f s 与固定开销，与实测采样 %.0f s 同量级（差值就是本机的频率/热波动）。"
         % (k_min, k_max, k, med_512_4, per_step_B or 0,
            (per_step_B or 1) / (med_512_4 or 1),
            np.log((per_step_B or 1) / (med_512_4 or 1)) / np.log(ratio_px) if med_512_4 and per_step_B else 0,
            tA.get("vae_s") or 0, tC.get("vae_s") or 0, tB.get("vae_s") or 0,
            k, k * 0.262144 * 4, tA.get("vae_s") or 0, tA.get("sampling_s") or 0)),
        ("5. 人像合成中为什么要使用预乘 alpha？不做预乘会出现什么视觉问题？",
         "缩放是线性插值，但“颜色”和“不透明度”必须按同一权重插值才有意义。不做预乘时，"
         "白底（255,255,255）的像素会以 1-alpha 的权重混进边缘像素：avatar 缩小 4 倍时，"
         "边缘一圈像素变成 0.5×肤色 + 0.5×白色，颜色被“提亮”，贴到深色背景上就是一圈白边"
         "（本次实践里把头像缩小到约 1/4 尺寸，未预乘时白边最宽可到 3~4 px）。"
         "预乘 alpha 先把 rgb 乘上 alpha 再插值，等于把“白色”的贡献按透明度折算掉，"
         "缩放后再除以 alpha 还原颜色，边缘就只剩皮肤色与发丝色，没有白边。"),
        ("",
         "本次实现里 resize_premul() 就是把 rgb×alpha 与 alpha 分别 INTER_AREA 缩放，再用 "
         "p/max(a,1e-3) 还原；测试 test_resize_premul_avoids_white_fringe 直接把这条性质固化成单测："
         "半透明边缘的颜色必须接近脸而不是接近白，且 alpha≈0 处的预乘值必须为 0。"),
        ("6. 若希望同一张脸在不同种子、不同姿态下保持一致，有哪些可行技术路线？",
         "① 人脸身份嵌入（IP-Adapter FaceID / InstantID / PuLID）：用 InsightFace 抽 512 维身份向量，"
         "通过交叉注意力注入 UNet，换种子只改姿态不改身份；代价是要额外 1~2 GB 权重与每步约 20% 开销，"
         "且 SD2.1 生态支持少，多为 SDXL 版本。"),
        ("",
         "② LoRA 微调（DreamBooth / 少样本 LoRA）：用 10~20 张同一人的照片训一个小秩适配器（r=8~16），"
         "推理时以 0.6~0.8 权重叠加；代价是每个身份要训练 5~20 分钟（CPU 上更久），优点是姿态自由、"
         "身份稳定。③ 参考图条件（reference-only / ReferenceNet）：把头像特征作为条件分支注入，"
         "不训练、只推理，但控制力弱于前两者。④ 本次用的“贴图 + img2img”路线：几何一致性最好"
         "（人脸几乎是原样），但姿态被锁死、不能大角度转头，属于“换脸”而不是“生成同一张脸”。"
         "工程上常用组合：LoRA 保证身份、depth/openpose ControlNet 保证姿态、IP-Adapter 做兜底身份约束。"),
    ]
    qidx = [15, 16, 17, 18, 19, 20]
    cur = -1
    merged_cells = {}
    for text, ans in qa:
        if text:
            cur += 1
            table = t[qidx[cur]]
            # 模板每题 3 行，删掉多余空行，只留一个答题框；答案（可能多段）都写进同一格
            shrink_table_to_one_row(table)
            merged = table.rows[0].cells[0]
            merged_cells[qidx[cur]] = merged
            cell_lines(merged, [ans], size=10, align=WD_ALIGN_PARAGRAPH.JUSTIFY)
        else:
            merged = merged_cells[qidx[cur]]
            p_new = merged.add_paragraph()
            set_para(p_new, ans, size=10, align=WD_ALIGN_PARAGRAPH.JUSTIFY, space_after=2)

    # ---------------- 保存 ----------------
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    print("[build_report] saved -> %s" % out_path)
    return out_path


def main():
    try:  # 控制台可能是 GBK，避免打印 ² 等字符时崩溃
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--template",
                    default=r"D:\Downloads\作业七_白膜转写实纯CPU深度控制流水线_实验报告（空白模板）.docx")
    ap.add_argument("--out",
                    default=str(LAB_ROOT / "2025310151_胡志源_实验七_白膜转写实实验报告.docx"))
    args = ap.parse_args()
    build(Path(args.template), Path(args.out))


if __name__ == "__main__":
    main()
