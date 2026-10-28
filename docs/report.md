# 白膜转写实：纯 CPU 深度控制生成流水线（实验七技术报告）

> 本文是仓库内的技术小结，完整版实验报告见 `../2025310151_胡志源_实验七_白膜转写实实验报告.docx`。
> 所有数字来自 `results/*.json` 与 `results/summary.md`，由 `scripts/analyze_runs.py` 自动汇总。

## 1. 一句话结论

用 Blender 无头渲染出 depth/normal/clay 三张图，把 depth 作为 SD2.1 depth ControlNet 的条件，
在**纯 CPU**（Intel Core Ultra 9 185H，无 GPU）上用 `stable-diffusion.cpp` 跑 SD-Turbo q4_0，
512²/4 步 **66.1 s 采样 + 18.0 s VAE（总 98.2 s）**，四个白模的轮廓、遮挡与透视都被完整迁移
（白模边缘的 98.3% 在 4 px 内被写实图接住），代价是材质语义漂移（球被画成陶罐、立方体变成铁罐）。

## 2. 环境

| 项目 | 配置 |
|---|---|
| CPU / 内存 | Intel Core Ultra 9 185H（16 核 22 线程，P+E）/ 40 GB（纯 CPU，GPU 未参与） |
| OS | Windows 11 Pro 10.0.26200 |
| Blender | 4.5.13 LTS，`blender -b` 无头 + Cycles CPU，64 采样 + 降噪 |
| 推理后端 | stable-diffusion.cpp `master-1017-be48649`（win-cpu-x64 预编译，线程 `-1`） |
| 主模型 | `sd_turbo-q4_0.gguf`（1762.3 MB，q4_0，内嵌 SD2.1 CLIP-H 与 VAE） |
| ControlNet | `control_depth_sd21-fp16.gguf`（694.7 MB，thibaud/controlnet-sd21 的 depth，fp16） |

## 3. 结果

### 3.1 参数矩阵与耗时（同一安静时段、高优先级顺序执行）

| 运行 | 分辨率 | 步数 | CFG | CN | 采样(s) | 单步(s) | VAE(s) | 总耗时(s) | 白模边缘中位偏离(px) | ≤4px 覆盖率 |
|---|---|---|---|---|---|---|---|---|---|---|
| A | 512² | 4 | 1.2 | 0.9 | 66.1 | 16.5 | 18.0 | 98.2 | 0.95 | 98.3% |
| B | 768² | 4 | 1.2 | 0.9 | 122.6 | 30.6 | 56.5 | 205.4 | 0.95 | 89.6% |
| C | 512² | 8 | 1.2 | 0.9 | 97.7 | 12.2 | 16.1 | 129.0 | 0.95 | 93.2% |
| D | 512² | 4 | 1.2 | 0.5 | 57.4 | 14.3 | 15.1 | 86.8 | 0.95 | 92.9% |
| E | 512² | 4 | 2.5 | 0.9 | 50.4 | 12.6 | 25.0 | 88.3 | 0.95 | 92.5% |
| F | 512² | 4 | 5.0 | 0.9 | 62.3 | 15.6 | 15.5 | 90.8 | 1.37 | 69.9% |

- 单步耗时 ≈ 53 s/Mpx × 像素数（六组折算落在 47~63 s/Mpx）；像素 ×2.25 只让单步 ×1.85。
- 采样耗时正比于步数（4→8 步：66.1→97.7 s），VAE 只与分辨率有关（512² ≈ 15~18 s、768² ≈ 56.5 s）。
- Blender 段 3~9 s：一次渲染同时产出 depth / normal / clay。

### 3.2 几何与质量

- **结构级一致**：四个物体的数量、相对位置、遮挡关系、透视线与接触阴影方向全部与白模一致；
  圆环保持通孔且椭圆方向正确。
- **细节级退化**：倒角立方体的 R7 mm 圆角被抹平；圆环管径被画细、截面压扁（更像“盘子”）。
- **材质语义漂移**：CFG 1.2 + 不带名词的提示词下，模型用“桌面器物”先验填色，四个白模都成了容器。
- **明确的失败样本**：
  - D（CN 0.5）：圆环通孔被封成一块反光盖片、立方体长出一条盖缝——拓扑被改写（覆盖率仍有 92.9%，
    说明轮廓类指标看不到拓扑错误）。
  - E/F（CFG 2.5/5.0）：E 过饱和成砖红/琥珀色，F 高光整片溢出、暗部死黑，几何覆盖率掉到 69.9%。
- **img2img 强度**：合成头像后用 strength 0.35 会把人脸完全“愈合”掉，0.18 才能保留。

## 4. 关键实现

1. **深度图 = 1 − mist**，并按“可见表面射线采样的 2% 分位（近）+ 非地面物体顶点的 98% 分位（远）”
   设定 mist 起止（5.17~8.90 m）。只统计顶点会得到 7.63~8.76 m，画面下沿被截断成死白。
2. **一次渲染出三图**：compositor 里 `Mist→Invert→OutputFile(depth)`、
   `Normal→×0.5+0.5→OutputFile(normal)`、`Image→Composite` 落成 clay。
3. **control image 强制 RGB**（sd.cpp 需要 ≥3 通道），相对路径一律转绝对路径（sd-cli 的工作目录是 vendor/）。
4. **预乘 alpha** 贴头像：`rgb×alpha` 缩放后再除以缩放后的 alpha，避免白边；
   接触阴影 + `cv2.seamlessClone` + 颗粒匹配，最后用 strength 0.18 的 img2img 统一光色。

## 5. 复现

```powershell
pip install -r requirements.txt
set BLENDER_EXE=D:\tools\blender-4.5.13-windows-x64\blender.exe
python scripts/fetch_assets.py --proxy http://127.0.0.1:7897   # 2.4 GB
powershell -NoProfile -File scripts/run_all.ps1
python scripts/analyze_runs.py
python scripts/build_report.py      # 需要实验报告模板 docx
pytest -q
```
