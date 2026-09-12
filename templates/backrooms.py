"""
Backrooms scene template - a procedural liminal-space maze (Cycles).

Usage (headless):
    blender -b --factory-startup -P backrooms.py -- params.json out_dir

Receives exactly two positional arguments after "--":
    argv[1] : path to a params.json file (see below)
    argv[2] : output directory (backrooms.mp4 and timing.txt are written here)

Parameters (all optional, clamped to safe ranges):
    seconds   float, 1..30     animation duration          [10]
    fps       int,   12..30    frames per second           [24]
    width     int,   320..3840 video width, 16:9, even     [1280]
    quality   str             draft | good | final         [good]
    seed      int             maze randomness seed         [42]
    grid      int,   6..14    maze grid size (NxN)         [10]
    engine    str             cycles | eevee               [cycles]
    denoise   bool            OpenImageDenoise on/off      [true]
"""
import bpy
import json
import math
import os
import random
import sys
import time

argv = sys.argv[sys.argv.index("--") + 1:]
params_path, out_dir = argv[0], argv[1]
P = json.load(open(params_path))

SECONDS = min(max(float(P.get("seconds", 10)), 1), 30)
FPS = int(min(max(int(P.get("fps", 24)), 12), 30))
WIDTH = int(min(max(int(P.get("width", 1280)), 320), 3840))
HEIGHT = int(WIDTH * 9 / 16)
# H.264 requires even dimensions
WIDTH -= WIDTH % 2
HEIGHT -= HEIGHT % 2
QUALITY = P.get("quality", "good")
SEED = int(P.get("seed", 42))
GRID = int(min(max(int(P.get("grid", 10)), 6), 14))
ENGINE = P.get("engine", "cycles")

SAMPLES = {"draft": 24, "good": 96, "final": 256}[QUALITY if QUALITY in
          ("draft", "good", "final") else "good"]

t0 = time.time()
random.seed(SEED)
bpy.ops.wm.read_factory_settings(use_empty=True)

CELL, N, WALL_H = 4.0, GRID, 3.0
CENTER = (GRID // 2, GRID // 2)

# ---------- Materials (all procedural, no textures needed) ----------
def new_mat(name):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    return m, m.node_tree, m.node_tree.nodes["Principled BSDF"]

# Mustard-yellow wallpaper: noise color variation + subtle bump
wall_mat, wnt, wbsdf = new_mat("Wallpaper")
wn = wnt.nodes.new('ShaderNodeTexNoise')
wn.inputs['Scale'].default_value = 3.0
wn.inputs['Detail'].default_value = 8.0
wmix = wnt.nodes.new('ShaderNodeMix')
wmix.data_type = 'RGBA'
wmix.inputs[6].default_value = (0.62, 0.53, 0.22, 1)
wmix.inputs[7].default_value = (0.52, 0.44, 0.17, 1)
wlk = wnt.links
wlk.new(wn.outputs['Fac'], wmix.inputs['Factor'])
wlk.new(wmix.outputs[2], wbsdf.inputs['Base Color'])
wbump = wnt.nodes.new('ShaderNodeBump')
wbump.inputs['Strength'].default_value = 0.08
wlk.new(wn.outputs['Fac'], wbump.inputs['Height'])
wlk.new(wbump.outputs['Normal'], wbsdf.inputs['Normal'])
wbsdf.inputs['Roughness'].default_value = 0.85

# Damp-looking carpet
car_mat, cnt, cbsdf = new_mat("Carpet")
cn = cnt.nodes.new('ShaderNodeTexNoise')
cn.inputs['Scale'].default_value = 60.0
cmix = cnt.nodes.new('ShaderNodeMix')
cmix.data_type = 'RGBA'
cmix.inputs['Factor'].default_value = 0.5
cmix.inputs[6].default_value = (0.30, 0.25, 0.11, 1)
cmix.inputs[7].default_value = (0.22, 0.18, 0.08, 1)
clk = cnt.links
clk.new(cn.outputs['Fac'], cmix.inputs['Factor'])
clk.new(cmix.outputs[2], cbsdf.inputs['Base Color'])
cbump = cnt.nodes.new('ShaderNodeBump')
cbump.inputs['Strength'].default_value = 0.5
clk.new(cn.outputs['Fac'], cbump.inputs['Height'])
clk.new(cbump.outputs['Normal'], cbsdf.inputs['Normal'])
cbsdf.inputs['Roughness'].default_value = 1.0

# Ceiling tiles
ceil_mat, ent, ebsdf = new_mat("Ceiling")
etex = ent.nodes.new('ShaderNodeTexCoord')
ebrick = ent.nodes.new('ShaderNodeTexBrick')
ebrick.inputs['Scale'].default_value = 1.0
ebrick.inputs['Color1'].default_value = (0.72, 0.70, 0.64, 1)
ebrick.inputs['Color2'].default_value = (0.68, 0.66, 0.60, 1)
ebrick.inputs['Mortar'].default_value = (0.35, 0.33, 0.28, 1)
ebrick.inputs['Mortar Size'].default_value = 0.006
ebrick.inputs['Brick Width'].default_value = 1.2
ebrick.inputs['Row Height'].default_value = 0.6
ent.links.new(etex.outputs['Object'], ebrick.inputs['Vector'])
ent.links.new(ebrick.outputs['Color'], ebsdf.inputs['Base Color'])
ebsdf.inputs['Roughness'].default_value = 0.9

# Fluorescent light panels (emissive), with a dim variant
light_mat, lnt, lbsdf = new_mat("Neon")
lbsdf.inputs['Emission Color'].default_value = (1.0, 0.96, 0.82, 1)
lbsdf.inputs['Emission Strength'].default_value = 14.0
light_mat_dim = light_mat.copy()
light_mat_dim.node_tree.nodes["Principled BSDF"].inputs['Emission Strength'].default_value = 1.5

# ---------- Floor / ceiling ----------
def big_plane(z, mat):
    bpy.ops.mesh.primitive_plane_add(size=N * CELL, location=(N * CELL / 2, N * CELL / 2, z))
    bpy.context.object.data.materials.append(mat)

big_plane(0.0, car_mat)
big_plane(WALL_H, ceil_mat)

# ---------- Maze: random walls & pillars, camera area stays open ----------
def wall(x, y, along_x):
    bpy.ops.mesh.primitive_cube_add(size=1, location=(x, y, WALL_H / 2))
    o = bpy.context.object
    o.scale = (CELL, 0.15, WALL_H) if along_x else (0.15, CELL, WALL_H)
    o.data.materials.append(wall_mat)

def near_cam(i, j):
    return abs(i - CENTER[0]) <= 1 and abs(j - CENTER[1]) <= 1

wall(N * CELL / 2, 0.075, True)
wall(N * CELL / 2, N * CELL - 0.075, True)
wall(0.075, N * CELL / 2, False)
wall(N * CELL - 0.075, N * CELL / 2, False)
for i in range(N):
    for j in range(N):
        if i < N - 1 and j != CENTER[1] and not near_cam(i, j) and not near_cam(i + 1, j):
            if random.random() < 0.35:
                wall((i + 1) * CELL, j * CELL + CELL / 2, False)
        if j < N - 1 and not near_cam(i, j) and not near_cam(i, j + 1):
            if random.random() < 0.35:
                wall(i * CELL + CELL / 2, (j + 1) * CELL, True)
for i in range(1, N):
    for j in range(1, N):
        if near_cam(i, j) or near_cam(i - 1, j) or near_cam(i, j - 1) or near_cam(i - 1, j - 1):
            continue
        if random.random() < 0.18:
            bpy.ops.mesh.primitive_cube_add(size=1, location=(i * CELL, j * CELL, WALL_H / 2))
            o = bpy.context.object
            o.scale = (0.5, 0.5, WALL_H)
            o.data.materials.append(wall_mat)

# ---------- Light panels: 15% dead, 15% dim, rest normal ----------
for i in range(N):
    for j in range(N):
        bpy.ops.mesh.primitive_plane_add(size=1, location=(i * CELL + CELL / 2, j * CELL + CELL / 2, WALL_H - 0.02))
        o = bpy.context.object
        o.scale = (1.0, 0.45, 1.0)
        r = random.random()
        if r < 0.15:
            continue
        o.data.materials.append(light_mat_dim if r < 0.30 else light_mat)

# ---------- Camera: slow forward drift (handheld found-footage feel) ----------
bpy.ops.object.camera_add(location=(CENTER[0] * CELL + 1.0, CENTER[1] * CELL + 2.0, 1.55),
                          rotation=(math.pi / 2 - 0.10, 0, -math.pi / 2))
cam = bpy.context.object
cam.data.lens = 17
bpy.context.scene.camera = cam

scene = bpy.context.scene
scene.render.fps = FPS
frame_total = int(SECONDS * FPS)
scene.frame_start = 1
scene.frame_end = frame_total

cam.keyframe_insert(data_path="location", frame=1)
cam.location.y -= 6.0
cam.keyframe_insert(data_path="location", frame=frame_total)

world = bpy.data.worlds.new("Gloom")
world.use_nodes = True
world.node_tree.nodes["Background"].inputs[0].default_value = (0.05, 0.042, 0.025, 1)
scene.world = world

# ---------- Render settings ----------
if ENGINE == "eevee":
    scene.render.engine = 'BLENDER_EEVEE_NEXT'
else:
    scene.render.engine = 'CYCLES'
    scene.cycles.samples = SAMPLES
    scene.cycles.use_denoising = bool(P.get("denoise", True))
    try:
        scene.cycles.denoiser = 'OPENIMAGEDENOISE'
    except Exception:
        pass
    # Use a GPU when one is available (Metal on macOS, OptiX/CUDA elsewhere),
    # otherwise fall back to CPU automatically.
    used_gpu = False
    try:
        prefs = bpy.context.preferences.addons['cycles'].preferences
        for dev_type in ('METAL', 'OPTIX', 'CUDA', 'HIP', 'ONEAPI'):
            try:
                prefs.compute_device_type = dev_type
                prefs.get_devices()
            except Exception:
                continue
            if any(d.type != 'CPU' for d in prefs.devices):
                for d in prefs.devices:
                    d.use = d.type != 'CPU'
                scene.cycles.device = 'GPU'
                used_gpu = True
                print(f"GPU: {dev_type}")
                break
    except Exception:
        pass
    if not used_gpu:
        scene.cycles.device = 'CPU'
        print("GPU: none - rendering on CPU")

scene.render.resolution_x = WIDTH
scene.render.resolution_y = HEIGHT
scene.view_settings.exposure = -0.15

ims = scene.render.image_settings
if bpy.app.version < (5, 0, 0):
    ims.file_format = 'FFMPEG'
    scene.render.ffmpeg.format = 'MPEG4'
    scene.render.ffmpeg.codec = 'H264'
    scene.render.ffmpeg.constant_rate_factor = 'HIGH'
else:
    # Blender 5.x+ moved video output to media_type
    ims.media_type = 'VIDEO'
    try:
        scene.render.ffmpeg.format = 'MPEG4'
        scene.render.ffmpeg.codec = 'H264'
    except Exception:
        pass  # video format defaults are fine

scene.render.filepath = os.path.join(out_dir, "backrooms.mp4")

t1 = time.time()
bpy.ops.render.render(animation=True)
t2 = time.time()

with open(os.path.join(out_dir, "timing.txt"), "w") as f:
    f.write(f"setup={t1-t0:.1f}s render={t2-t1:.1f}s frames={frame_total} engine={ENGINE}\n")
print(f"RENDER_DONE setup={t1-t0:.1f}s render={t2-t1:.1f}s")
