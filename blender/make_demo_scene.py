# -*- coding: utf-8 -*-
"""生成演示白模场景（球、圆环、倒角立方体、圆柱）并保存为 .blend。

推荐在 Blender 内执行（Blender 4.5.13 LTS）：

    blender -b -P blender/make_demo_scene.py -- --out assets/demo_white_model.blend --res 512

也支持直接用普通 Python 调用（脚本会自动找到 Blender 再 re-exec 自己）：

    python blender/make_demo_scene.py --blender "C:/Program Files/Blender Foundation/Blender 4.5/blender.exe"

场景内容：白色哑光材质的四个几何体 + 地面 + 三点布光（Key/Fill/Rim）+ 63mm 相机，
Cycles CPU 渲染，64 采样 + 降噪。
"""
from __future__ import annotations

import os
import subprocess
import sys

# --------------------------------------------------------------------------------------
# 不在 Blender 内时：自动 re-exec 到 Blender（-b 无头模式）
# --------------------------------------------------------------------------------------
try:
    import bpy  # type: ignore  # noqa: F401
    from mathutils import Vector  # type: ignore
    _IN_BLENDER = True
except ImportError:  # pragma: no cover - 仅在普通 python 下触发
    _IN_BLENDER = False


def _find_blender() -> str | None:
    cand = os.environ.get("BLENDER_EXE")
    if cand and os.path.isfile(cand):
        return cand
    for arg in sys.argv[1:]:
        if arg.startswith("--blender="):
            path = arg.split("=", 1)[1]
            if os.path.isfile(path):
                return path
    return None


def _reboot_in_blender() -> None:
    exe = _find_blender()
    if not exe:
        sys.stderr.write(
            "未找到 bpy，且没有指定 Blender 可执行文件。\n"
            "请用下面任一方式运行：\n"
            "  blender -b -P blender/make_demo_scene.py -- --out assets/demo_white_model.blend\n"
            "  set BLENDER_EXE=C:\\path\\to\\blender.exe && python blender/make_demo_scene.py\n"
        )
        raise SystemExit(2)
    cmd = [exe, "-b", "--factory-startup", "-P", os.path.abspath(__file__), "--"] + sys.argv[1:]
    print("[make_demo_scene] re-exec through Blender: %s" % " ".join(cmd))
    raise SystemExit(subprocess.call(cmd))


# --------------------------------------------------------------------------------------
# Blender 内部逻辑
# --------------------------------------------------------------------------------------
def parse_args(argv):
    import argparse

    p = argparse.ArgumentParser(description="build the demo white-model scene")
    p.add_argument("--out", default="assets/demo_white_model.blend", help="输出 .blend 路径")
    p.add_argument("--res", type=int, default=512, help="渲染分辨率（正方形），512 或 768")
    p.add_argument("--samples", type=int, default=64, help="Cycles 采样数")
    p.add_argument("--preset", choices=["demo", "figure"], default="demo",
                   help="demo=四个几何体；figure=滑冰姿态人形（拓展项用）")
    p.add_argument("--blender", default=None, help="（可选）Blender 可执行文件路径")
    args = argv[argv.index("--") + 1:] if "--" in argv else []
    return p.parse_args(args)


def look_at(obj, target):
    direction = Vector(target) - obj.location
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def add_area_light(name, location, energy, size, target, color=(1.0, 1.0, 1.0)):
    data = bpy.data.lights.new(name, type="AREA")
    data.energy, data.size = energy, size
    data.color = color
    obj = bpy.data.objects.new(name, data)
    bpy.context.collection.objects.link(obj)
    obj.location = location
    look_at(obj, target)
    return obj


def matte(name, base_color, roughness=0.5, metallic=0.0):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = (*base_color, 1.0)
        bsdf.inputs["Roughness"].default_value = roughness
        bsdf.inputs["Metallic"].default_value = metallic
    return mat


def setup_world(scene, strength=0.03):
    world = bpy.data.worlds.new("World")
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs["Color"].default_value = (0.35, 0.37, 0.40, 1.0)
        bg.inputs["Strength"].default_value = strength
    # Mist（深度）通道的起止距离在导出脚本里按可见顶点 2%/98% 百分位重设
    # Mist（深度）通道：Blender 用 start + depth 描述范围，导出脚本会按 2%/98% 百分位重设
    world.mist_settings.use_mist = True
    world.mist_settings.start = 4.0
    world.mist_settings.depth = 20.0
    world.mist_settings.falloff = "LINEAR"
    scene.world = world


def add_plane(size, location, material, name="Floor"):
    bpy.ops.mesh.primitive_plane_add(size=size, location=location)
    obj = bpy.context.object
    obj.name = name
    obj.data.materials.append(material)
    obj["is_ground"] = True  # 导出脚本据此把地面排除在“物体深度”统计之外
    return obj


def add_sphere(radius, location, material, name, segments=64, rings=32):
    bpy.ops.mesh.primitive_uv_sphere_add(radius=radius, segments=segments,
                                         ring_count=rings, location=location)
    obj = bpy.context.object
    obj.name = name
    bpy.ops.object.shade_smooth()
    obj.data.materials.append(material)
    return obj


def add_torus(major, minor, location, rotation, material, name):
    bpy.ops.mesh.primitive_torus_add(major_radius=major, minor_radius=minor,
                                     major_segments=64, minor_segments=24,
                                     location=location, rotation=rotation)
    obj = bpy.context.object
    obj.name = name
    bpy.ops.object.shade_smooth()
    obj.data.materials.append(material)
    return obj


def add_bevel_cube(size, location, rotation, bevel, material, name):
    bpy.ops.mesh.primitive_cube_add(size=size, location=location, rotation=rotation)
    obj = bpy.context.object
    obj.name = name
    mod = obj.modifiers.new("Bevel", "BEVEL")
    mod.width = bevel
    mod.segments = 4
    mod.limit_method = "ANGLE"
    obj.data.materials.append(material)
    return obj


def add_cylinder(radius, depth, location, rotation, material, name, vertices=64):
    bpy.ops.mesh.primitive_cylinder_add(radius=radius, depth=depth, vertices=vertices,
                                        location=location, rotation=rotation)
    obj = bpy.context.object
    obj.name = name
    mod = obj.modifiers.new("Bevel", "BEVEL")
    mod.width = min(0.04, radius * 0.2)
    mod.segments = 3
    obj.data.materials.append(material)
    return obj


def build_demo(body_mat, floor_mat):
    """四个几何体沿“屏幕右方向”等距排开，避免 3/4 视角下互相遮挡、近大远小。"""
    add_plane(24, (0, 0, 0), floor_mat)
    # 相机位于 (4.8, -6.0, 2.8) 看向原点，屏幕右方向 ≈ (0.78, 0.62)；
    # 沿该方向排布能让四个物体到相机的距离基本相同，构图更整齐。
    ux, uy = 0.78, 0.62
    add_sphere(0.55, (-1.8 * ux, -1.8 * uy, 0.55), body_mat, "Sphere")
    add_torus(0.44, 0.15, (-0.6 * ux, -0.6 * uy, 0.62), (1.10, 0.30, 0.20), body_mat, "Torus")
    add_bevel_cube(0.95, (0.6 * ux, 0.6 * uy, 0.48), (0.0, 0.0, 0.40), 0.07, body_mat, "RoundedCube")
    add_cylinder(0.30, 1.00, (1.8 * ux, 1.8 * uy, 0.50), (0.0, 0.0, 0.0), body_mat, "Cylinder")


def add_box(dims, location, rotation, bevel, material, name):
    """非等边倒角方盒（用 scale 做出躯干/髋部的比例，再 apply 后倒角）。"""
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=location, rotation=rotation)
    obj = bpy.context.object
    obj.name = name
    obj.scale = (dims[0], dims[1], dims[2])  # primitive_cube_add(size=1) 的边长是 1，故直接按边长缩放
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    mod = obj.modifiers.new("Bevel", "BEVEL")
    mod.width = bevel
    mod.segments = 4
    mod.limit_method = "ANGLE"
    obj.data.materials.append(material)
    return obj


def add_bone(p1, p2, radius, material, name, vertices=32):
    """在两个关节点之间生成一根圆柱（保证四肢与躯干是连续的）。"""
    a, b = Vector(p1), Vector(p2)
    d = b - a
    length = max(d.length, 1e-4)
    bpy.ops.mesh.primitive_cylinder_add(radius=radius, depth=length, vertices=vertices,
                                        location=(a + b) / 2.0)
    obj = bpy.context.object
    obj.name = name
    obj.rotation_mode = "QUATERNION"
    obj.rotation_quaternion = d.to_track_quat("Z", "Y")
    bpy.ops.object.shade_smooth()
    obj.data.materials.append(material)
    return obj


def joint(p, radius, material, name):
    return add_sphere(radius, p, material, name, segments=32, rings=16)


def build_figure(body_mat, floor_mat):
    """滑冰姿态的简化人形（骨肉相连的木偶），供拓展项“头像融合”做底图。

    用 add_bone() 把“关节坐标 → 圆柱”自动摆好，避免手写欧拉角导致肢体断裂。
    """
    add_plane(24, (0, 0, 0), floor_mat)

    pelvis, chest = (0.0, 0.0, 0.95), (0.0, 0.0, 1.34)
    neck_a, neck_b = (0.0, 0.0, 1.36), (0.0, 0.0, 1.52)
    head_c = (0.0, -0.015, 1.63)

    add_box((0.40, 0.24, 0.46), (0.0, 0.0, 1.15), (0.0, 0.06, 0.0), 0.05, body_mat, "Torso")
    add_box((0.32, 0.22, 0.18), (0.0, 0.0, 0.94), (0.0, 0.0, 0.0), 0.04, body_mat, "Hips")
    add_bone(neck_a, neck_b, 0.075, body_mat, "Neck")
    add_sphere(0.125, head_c, body_mat, "Head")

    # 左臂上扬、右臂后摆（滑冰姿态）
    sh_l, el_l, wr_l = (0.21, 0.0, 1.32), (0.42, -0.06, 1.47), (0.58, -0.14, 1.61)
    sh_r, el_r, wr_r = (-0.21, 0.0, 1.32), (-0.40, 0.06, 1.18), (-0.54, 0.16, 1.02)
    add_bone(sh_l, el_l, 0.055, body_mat, "ArmUpperL")
    add_bone(el_l, wr_l, 0.048, body_mat, "ArmLowerL")
    add_bone(sh_r, el_r, 0.055, body_mat, "ArmUpperR")
    add_bone(el_r, wr_r, 0.048, body_mat, "ArmLowerR")
    for name, p, r in (("ShoulderL", sh_l, 0.075), ("ElbowL", el_l, 0.058),
                       ("ShoulderR", sh_r, 0.075), ("ElbowR", el_r, 0.058),
                       ("WristL", wr_l, 0.05), ("WristR", wr_r, 0.05)):
        joint(p, r, body_mat, name)

    # 腿：左腿支撑、右腿后抬
    hip_l, knee_l, ankle_l = (0.10, 0.0, 0.92), (0.12, -0.02, 0.50), (0.13, -0.06, 0.07)
    hip_r, knee_r, ankle_r = (-0.10, 0.0, 0.92), (-0.13, 0.30, 0.80), (-0.15, 0.62, 0.92)
    add_bone(hip_l, knee_l, 0.078, body_mat, "LegUpperL")
    add_bone(knee_l, ankle_l, 0.062, body_mat, "LegLowerL")
    add_bone(hip_r, knee_r, 0.078, body_mat, "LegUpperR")
    add_bone(knee_r, ankle_r, 0.062, body_mat, "LegLowerR")
    for name, p, r in (("KneeL", knee_l, 0.078), ("KneeR", knee_r, 0.078),
                       ("AnkleL", ankle_l, 0.06), ("AnkleR", ankle_r, 0.06)):
        joint(p, r, body_mat, name)


def main():
    args = parse_args(sys.argv)
    bpy.ops.wm.read_factory_settings(use_empty=True)  # 清空默认 Cube / Light / Camera
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = args.samples
    scene.cycles.use_denoising = True
    scene.cycles.use_adaptive_sampling = True
    scene.cycles.max_bounces = 6
    scene.cycles.diffuse_bounces = 3
    scene.render.resolution_x = args.res
    scene.render.resolution_y = args.res
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.fps = 24
    # Standard 视图变换：保证 mist/normal 通道写进 8bit PNG 时不被 Filmic/AgX 二次压暗
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.look = "None"
    scene.view_settings.exposure = 0.0
    scene.view_settings.gamma = 1.0

    setup_world(scene)

    body_mat = matte("ClayWhite", (0.82, 0.82, 0.80), roughness=0.55)
    floor_mat = matte("FloorGrey", (0.42, 0.43, 0.45), roughness=0.75)

    if args.preset == "figure":
        build_figure(body_mat, floor_mat)
    else:
        build_demo(body_mat, floor_mat)

    camera_data = bpy.data.cameras.new("Camera")
    camera_data.lens = 63
    camera_data.sensor_width = 36.0
    camera_data.clip_start = 0.1
    camera_data.clip_end = 200.0
    camera = bpy.data.objects.new("Camera", camera_data)
    bpy.context.collection.objects.link(camera)
    if args.preset == "figure":
        camera.location = (1.85, -3.15, 1.80)
        look_at(camera, (0.0, 0.12, 1.02))
    else:
        camera.location = (4.8, -6.0, 2.8)
        look_at(camera, (0.0, 0.0, 0.55))
    scene.camera = camera

    add_area_light("Key", (3.0, -3.2, 4.2), 120, 3.5, (0, 0, 0.5), color=(1.0, 0.97, 0.92))
    add_area_light("Fill", (-3.6, -2.8, 2.2), 40, 5.0, (0, 0, 0.6), color=(0.88, 0.93, 1.0))
    add_area_light("Rim", (0.5, 3.5, 3.2), 90, 2.2, (0, 0, 0.8), color=(1.0, 1.0, 1.0))

    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    scene.render.filepath = os.path.join(os.path.dirname(out_path), "preview")
    bpy.ops.wm.save_as_mainfile(filepath=out_path)
    n_obj = len([o for o in scene.objects if o.type == "MESH"])
    print("[make_demo_scene] saved %s (%d mesh objects, %dx%d, %d samples)"
          % (out_path, n_obj, args.res, args.res, args.samples))


if __name__ == "__main__":
    if not _IN_BLENDER:
        _reboot_in_blender()
    main()
