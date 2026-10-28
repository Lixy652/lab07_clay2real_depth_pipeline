# -*- coding: utf-8 -*-
"""无头导出 depth / normal / clay 三张图。

用法：

    blender -b assets/demo_white_model.blend -P blender/export_control_maps.py -- \
        --outdir output --name demo --res 512 --samples 64

产物：
    <outdir>/<name>_depth_0001.png   深度图（反相：近亮远暗），ControlNet 控制图
    <outdir>/<name>_normal_0001.png  法线图（n*0.5+0.5 映射到 0~1 的 RGB）
    <outdir>/<name>_clay.png         白模参考图（材质整体覆盖为纯白）

实现要点：
1. 用相机视锥内可见顶点的 2%/98% 百分位估计 mist 起止距离，避免远处地面把动态范围拉爆；
2. Mist 经反相后输出（近处亮、远处暗），符合 depth ControlNet 的“越近越白”约定；
3. 法线 n(-1..1) 经 *0.5+0.5 映射到 0~1 再存 RGB；
4. 一次渲染同时产出三张图：compositor 的 OutputFile 写 depth/normal，
   Render Result 经 Composite 节点写 clay 参考图。
"""
from __future__ import annotations

import os
import sys

import bpy  # type: ignore
import numpy as np
from bpy_extras.object_utils import world_to_camera_view  # type: ignore


# --------------------------------------------------------------------------------------
# 参数
# --------------------------------------------------------------------------------------
def parse_args(argv):
    import argparse

    p = argparse.ArgumentParser(description="export depth / normal / clay control maps")
    p.add_argument("--outdir", default="output", help="输出目录")
    p.add_argument("--name", default="demo", help="文件名前缀")
    p.add_argument("--res", type=int, default=512, help="边长像素（512 / 768）")
    p.add_argument("--samples", type=int, default=64, help="Cycles 采样数")
    p.add_argument("--near-percentile", type=float, default=2.0)
    p.add_argument("--far-percentile", type=float, default=98.0)
    p.add_argument("--depth-margin", type=float, default=0.06,
                   help="mist 远近端各留的比例余量，避免贴脸/贴地元素被截断")
    args = argv[argv.index("--") + 1:] if "--" in argv else []
    return p.parse_args(args)


# --------------------------------------------------------------------------------------
# 深度范围估计
# --------------------------------------------------------------------------------------
def scene_depth_range(scene, camera, samples=96, near_pct=2.0, far_pct=98.0):
    """估算 mist（深度）的起止距离。

    做法分两步：
    1) 对整幅画面做 rays 采样（scene.ray_cast），得到所有“可见表面”的欧氏距离，
       用其 2% 分位作为近端 —— 这样贴近相机的地面也会进入统计；
    2) 用非地面物体的顶点距离的 98% 分位作为远端 —— 避免远处地面/地平线把
       动态范围拉爆（地面在掠射角上的距离可以到几十米）。

    只用顶点统计时，地面（一个只有 4 个顶点的大平面）会被视锥排除，近端被抬到
    7.6m 左右，画面下方会出现大片被截断的死白区域；改成“见表面 + 见物体”的
    混合口径后，地面与物体都能拿到连续的灰度层次。
    """
    depsgraph = bpy.context.evaluated_depsgraph_get()
    cam_loc = camera.matrix_world.translation.copy()
    rot = camera.matrix_world.to_3x3()
    corners = [rot @ v for v in camera.data.view_frame(scene=scene)]

    # --- 1) 全画面可见表面距离 ---
    surface = []
    for iy in range(samples):
        ty = (iy + 0.5) / samples
        left = corners[0].lerp(corners[3], ty)   # 左下 -> 左上
        right = corners[1].lerp(corners[2], ty)  # 右下 -> 右上
        for ix in range(samples):
            tx = (ix + 0.5) / samples
            direction = (left.lerp(right, tx)).normalized()
            hit, loc, _nrm, _idx, obj, _mat = scene.ray_cast(depsgraph, cam_loc, direction)
            if hit:
                surface.append((loc - cam_loc).length)

    # --- 2) 非地面物体的顶点距离 ---
    obj_dists = []
    for obj in scene.objects:
        if obj.type != "MESH" or obj.hide_render:
            continue
        if obj.get("is_ground") or obj.name.lower().startswith("floor"):
            continue
        obj_eval = obj.evaluated_get(depsgraph)
        for vert in obj_eval.data.vertices:
            world_co = obj_eval.matrix_world @ vert.co
            ndc = world_to_camera_view(scene, camera, world_co)
            if ndc.z <= 0 or not (0.0 <= ndc.x <= 1.0 and 0.0 <= ndc.y <= 1.0):
                continue
            obj_dists.append((world_co - cam_loc).length)

    if not surface:
        return 0.1, 100.0
    near = float(np.percentile(surface, near_pct))
    far = float(np.percentile(obj_dists, far_pct)) if obj_dists else float(np.percentile(surface, far_pct))
    if far <= near:
        far = near + max(1.0, 0.25 * near)
    return near, far


# --------------------------------------------------------------------------------------
# 材质 / compositor
# --------------------------------------------------------------------------------------
def make_clay_material():
    mat = bpy.data.materials.new("ClayOverride")
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = (0.92, 0.92, 0.92, 1.0)
        bsdf.inputs["Roughness"].default_value = 0.62
        bsdf.inputs["Metallic"].default_value = 0.0
    return mat


def _new_mix(tree, blend_type, fac, color2):
    """兼容 4.x 的 MixRGB 与新版 Mix 节点，返回 (node, in_a, in_b, out)。"""
    try:
        node = tree.nodes.new("CompositorNodeMixRGB")
        node.blend_type = blend_type
        node.inputs[0].default_value = fac
        node.inputs[2].default_value = color2
        return node, node.inputs[1], node.inputs[2], node.outputs[0]
    except RuntimeError:
        node = tree.nodes.new("ShaderNodeMix")
        node.data_type = "RGBA"
        node.blend_type = blend_type
        node.inputs["Factor"].default_value = fac
        # ShaderNodeMix 的 RGBA 输入/输出是第 6/7 与第 2 个插槽
        in_a = [s for s in node.inputs if s.name == "A" and s.type == "RGBA"][0]
        in_b = [s for s in node.inputs if s.name == "B" and s.type == "RGBA"][0]
        out = [s for s in node.outputs if s.name == "Result" and s.type == "RGBA"][0]
        in_b.default_value = color2
        return node, in_a, in_b, out


def _new_invert(tree):
    """Mist 反相节点：优先 Invert，其次用 MixRGB 的 SUBTRACT 实现 1-x。"""
    try:
        node = tree.nodes.new("CompositorNodeInvert")
        if hasattr(node, "invert_rgb"):
            node.invert_rgb = True
        if "Fac" in node.inputs:
            node.inputs["Fac"].default_value = 1.0
        if "Invert Color" in node.inputs:
            node.inputs["Invert Color"].default_value = True
        in_socket = node.inputs["Color"] if "Color" in node.inputs else node.inputs["Image"]
        return node, in_socket, node.outputs[0]
    except RuntimeError:
        node, in_a, in_b, out = _new_mix(tree, "SUBTRACT", 1.0, (1.0, 1.0, 1.0, 1.0))
        # SUBTRACT: in_a - in_b = 1 - mist
        return node, in_b, out


def setup_compositor(scene, outdir, name):
    scene.use_nodes = True
    tree = scene.node_tree
    tree.nodes.clear()

    rl = tree.nodes.new("CompositorNodeRLayers")
    rl.location = (-600, 0)

    # depth: mist 0..1 (near..far) -> invert -> 近白远黑
    invert, inv_in, inv_out = _new_invert(tree)
    invert.location = (-350, 150)

    # normal: n(-1..1) * 0.5 + 0.5
    mul, mul_a, mul_b, mul_out = _new_mix(tree, "MULTIPLY", 1.0, (0.5, 0.5, 0.5, 1.0))
    mul.location = (-350, -150)
    add, add_a, add_b, add_out = _new_mix(tree, "ADD", 1.0, (0.5, 0.5, 0.5, 1.0))
    add.location = (-150, -150)

    depth_out = tree.nodes.new("CompositorNodeOutputFile")
    depth_out.location = (150, 200)
    depth_out.base_path = outdir
    depth_out.file_slots[0].path = name + "_depth_"
    depth_out.format.file_format = "PNG"
    depth_out.format.color_mode = "RGB"
    depth_out.format.color_depth = "8"

    normal_out = tree.nodes.new("CompositorNodeOutputFile")
    normal_out.location = (150, -150)
    normal_out.base_path = outdir
    normal_out.file_slots[0].path = name + "_normal_"
    normal_out.format.file_format = "PNG"
    normal_out.format.color_mode = "RGB"
    normal_out.format.color_depth = "8"

    comp = tree.nodes.new("CompositorNodeComposite")
    comp.location = (150, 0)

    tree.links.new(rl.outputs["Mist"], inv_in)
    tree.links.new(inv_out, depth_out.inputs[0])
    tree.links.new(rl.outputs["Normal"], mul_a)
    tree.links.new(mul_out, add_a)
    tree.links.new(add_out, normal_out.inputs[0])
    tree.links.new(rl.outputs["Image"], comp.inputs["Image"])
    return tree


# --------------------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------------------
def main():
    args = parse_args(sys.argv)
    scene = bpy.context.scene
    camera = scene.camera
    if camera is None:
        raise SystemExit("场景里没有相机，无法导出控制图")

    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = args.samples
    scene.cycles.use_denoising = True
    scene.render.resolution_x = args.res
    scene.render.resolution_y = args.res
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.image_settings.color_depth = "8"
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.look = "None"
    scene.view_settings.exposure = 0.0
    scene.view_settings.gamma = 1.0

    near, far = scene_depth_range(scene, camera, near_pct=args.near_percentile,
                                  far_pct=args.far_percentile)
    margin = max(1e-3, args.depth_margin * (far - near))
    near -= margin
    far += margin
    if scene.world is None:
        scene.world = bpy.data.worlds.new("World")
    # Blender 的 mist 用 start + depth 表达范围（end = start + depth）
    scene.world.mist_settings.use_mist = True
    scene.world.mist_settings.start = near
    scene.world.mist_settings.depth = max(1e-3, far - near)
    scene.world.mist_settings.falloff = "LINEAR"

    view_layer = bpy.context.view_layer
    view_layer.use_pass_mist = True
    view_layer.use_pass_normal = True
    view_layer.material_override = make_clay_material()

    outdir = os.path.abspath(args.outdir)
    os.makedirs(outdir, exist_ok=True)
    setup_compositor(scene, outdir, args.name)

    clay_path = os.path.join(outdir, "%s_clay.png" % args.name)
    scene.render.filepath = clay_path
    bpy.ops.render.render(write_still=True)

    print("[export_control_maps] mist range = [%.3f, %.3f] m "
          "(near=%.0f%% of visible surfaces, far=%.0f%% of object vertices, +%.0f%% margin)"
          % (near, far, args.near_percentile, args.far_percentile, args.depth_margin * 100))
    print("[export_control_maps] clay   -> %s" % clay_path)
    for suffix in ("depth", "normal"):
        cands = sorted(
            f for f in os.listdir(outdir) if f.startswith("%s_%s_" % (args.name, suffix))
        )
        if cands:
            print("[export_control_maps] %-6s -> %s" % (suffix, os.path.join(outdir, cands[-1])))


if __name__ == "__main__":
    main()
