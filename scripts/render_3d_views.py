#!/usr/bin/env python3
"""
从 GLB 模型渲染多角度产品参考图
生成 5 张白底产品参考图（正面、侧面、45度等），带纹理
"""

import os
import sys
import math
import asyncio
from pathlib import Path
from typing import List, Tuple

import numpy as np
from PIL import Image
import matplotlib
matplotlib.use('Agg')
from mpl_toolkits.mplot3d import Axes3D
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
import trimesh

# 添加项目路径（基于本脚本位置推导，跨机器可移植）
_SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVICE_DIR))
from services.oss_service import OSSService
from config import settings

# 配置（可通过环境变量覆盖）
GLB_PATH = os.environ.get("GLB_PATH", str(_SERVICE_DIR / "scripts" / "output" / "model.glb"))
OUTPUT_DIR = os.environ.get("RENDER_OUTPUT_DIR", str(_SERVICE_DIR / "scripts" / "output" / "renders"))
RESOLUTION = (1024, 1024)

# 渲染角度配置：(名称, 方位角, 仰角, 距离倍数)
VIEW_ANGLES = [
    ("front", 90, 0, 1.5),        # 正面 (azim=90 是正面视角)
    ("right", 0, 0, 1.5),         # 右侧面
    ("left", 180, 0, 1.5),        # 左侧面
    ("front_right", 45, 15, 1.8), # 45度角（带一点俯视）
    ("top", 90, 90, 2.0),         # 俯视
]


def load_glb_model(glb_path: str):
    """加载 GLB 模型文件，返回场景和预计算的纹理颜色"""
    print(f"正在加载 GLB 模型: {glb_path}")
    scene = trimesh.load(glb_path, force='scene')
    
    if isinstance(scene, trimesh.Trimesh):
        scene = trimesh.scene.Scene(scene)
    
    print(f"模型加载成功，包含 {len(scene.geometry)} 个几何体")
    
    # 预计算每个几何体的面颜色
    face_colors_map = {}
    for name, geom in scene.geometry.items():
        face_colors = compute_face_colors(geom)
        face_colors_map[name] = face_colors
    
    return scene, face_colors_map


def compute_face_colors(geom):
    """计算几何体每个面的颜色（从纹理）"""
    face_colors = None
    
    # 检查是否有纹理
    if hasattr(geom, 'visual') and hasattr(geom.visual, 'material'):
        material = geom.visual.material
        
        if hasattr(material, 'baseColorTexture') and material.baseColorTexture is not None:
            texture_img = material.baseColorTexture
            texture_array = np.array(texture_img)
            tex_h, tex_w = texture_array.shape[:2]
            
            # 获取 UV 坐标
            if hasattr(geom.visual, 'uv') and geom.visual.uv is not None:
                uv_coords = np.array(geom.visual.uv)
                
                # 计算每个三角形的平均 UV 坐标
                face_uvs = uv_coords[geom.faces]  # (n_faces, 3, 2)
                face_uv_mean = np.mean(face_uvs, axis=1)  # (n_faces, 2)
                
                # UV 坐标转像素坐标（V坐标需要翻转）
                px = np.clip((face_uv_mean[:, 0] * (tex_w - 1)).astype(int), 0, tex_w - 1)
                py = np.clip(((1 - face_uv_mean[:, 1]) * (tex_h - 1)).astype(int), 0, tex_h - 1)
                
                # 获取颜色
                face_colors = texture_array[py, px] / 255.0
    
    return face_colors


def compute_scene_bounds(scene):
    """计算场景的边界框"""
    vertices = []
    for name, geom in scene.geometry.items():
        if hasattr(geom, 'vertices'):
            # 应用变换
            if name in scene.graph.nodes:
                transform = scene.graph.get(name, np.eye(4))[0]
                verts = np.array([transform[:3, :3] @ v + transform[:3, 3] for v in geom.vertices])
            else:
                verts = geom.vertices
            vertices.append(verts)
    
    if not vertices:
        return np.array([-1, -1, -1]), np.array([1, 1, 1])
    
    all_vertices = np.vstack(vertices)
    min_bounds = np.min(all_vertices, axis=0)
    max_bounds = np.max(all_vertices, axis=0)
    return min_bounds, max_bounds


def render_with_matplotlib_textured(scene, face_colors_map, resolution: Tuple[int, int], 
                                    angle_name: str, azimuth: float, elevation: float, 
                                    distance_mult: float) -> Image.Image:
    """使用 matplotlib 渲染带纹理的场景"""
    # 计算场景边界
    min_bounds, max_bounds = compute_scene_bounds(scene)
    center = (min_bounds + max_bounds) / 2
    size = np.max(max_bounds - min_bounds)
    
    print(f"  场景中心: {center}, 尺寸: {size}")
    
    # 创建图形
    fig = plt.figure(figsize=(resolution[0]/100, resolution[1]/100), dpi=100)
    fig.patch.set_facecolor('white')
    ax = fig.add_subplot(111, projection='3d')
    ax.set_facecolor('white')
    
    # 收集所有几何体
    for name, geom in scene.geometry.items():
        if hasattr(geom, 'vertices') and hasattr(geom, 'faces'):
            # 应用变换
            if name in scene.graph.nodes:
                transform = scene.graph.get(name, np.eye(4))[0]
                vertices = np.array([transform[:3, :3] @ v + transform[:3, 3] for v in geom.vertices])
            else:
                vertices = geom.vertices
            
            faces = geom.faces
            face_colors = face_colors_map.get(name)
            
            # 绘制 mesh
            if face_colors is not None:
                mesh = Poly3DCollection([vertices[f] for f in faces], alpha=1.0)
                mesh.set_facecolor(face_colors)
                mesh.set_edgecolor('none')
            else:
                mesh = Poly3DCollection([vertices[f] for f in faces], alpha=0.9)
                mesh.set_facecolor('lightgray')
                mesh.set_edgecolor('none')
            
            ax.add_collection3d(mesh)
    
    # 设置视角
    ax.view_init(elev=elevation, azim=azimuth)
    
    # 设置坐标范围
    max_range = size * 0.7
    ax.set_xlim([center[0] - max_range, center[0] + max_range])
    ax.set_ylim([center[1] - max_range, center[1] + max_range])
    ax.set_zlim([center[2] - max_range, center[2] + max_range])
    
    # 隐藏坐标轴
    ax.set_axis_off()
    
    # 保存到内存
    import io
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=100, bbox_inches='tight', 
                pad_inches=0, facecolor='white')
    buf.seek(0)
    image = Image.open(buf)
    plt.close(fig)
    
    return image


def render_view(scene, face_colors_map, angle_name: str, azimuth: float, elevation: float, 
                distance_mult: float, resolution: Tuple[int, int]) -> Image.Image:
    """渲染单个视角"""
    print(f"  渲染 {angle_name}: 方位角={azimuth}°, 仰角={elevation}°")
    return render_with_matplotlib_textured(scene, face_colors_map, resolution, 
                                           angle_name, azimuth, elevation, distance_mult)


async def upload_to_oss(image_path: str, angle_name: str) -> str:
    """上传渲染图到 OSS"""
    oss_service = OSSService(
        access_key_id=settings.OSS_ACCESS_KEY_ID,
        access_key_secret=settings.OSS_ACCESS_KEY_SECRET,
        bucket_name=settings.OSS_BUCKET_NAME,
        endpoint=settings.OSS_ENDPOINT,
        cdn_domain=settings.OSS_CDN_DOMAIN,
    )
    
    oss_key = f"3d_models/{os.environ.get('PROJECT_ID', 'unspecified')}/render_{angle_name}.png"
    url = await oss_service.upload_file(
        local_path=image_path,
        oss_key=oss_key,
        content_type="image/png",
        expires=7200
    )
    
    return url


async def main():
    """主函数"""
    print("=" * 60)
    print("GLB 模型多角度渲染工具（带纹理）")
    print("=" * 60)
    
    # 检查模型文件
    if not os.path.exists(GLB_PATH):
        print(f"错误: 模型文件不存在: {GLB_PATH}")
        return
    
    # 创建输出目录
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print(f"输出目录: {OUTPUT_DIR}")
    
    # 加载模型和纹理
    scene, face_colors_map = load_glb_model(GLB_PATH)
    
    # 渲染所有角度
    rendered_images = []
    
    for angle_name, azimuth, elevation, distance_mult in VIEW_ANGLES:
        print(f"\n渲染视角: {angle_name}")
        
        try:
            image = render_view(scene, face_colors_map, angle_name, azimuth, elevation, 
                              distance_mult, RESOLUTION)
            
            # 保存图片
            output_path = os.path.join(OUTPUT_DIR, f"{angle_name}.png")
            image.save(output_path, "PNG")
            print(f"  已保存: {output_path}")
            
            rendered_images.append((angle_name, output_path))
            
        except Exception as e:
            print(f"  渲染失败: {e}")
            import traceback
            traceback.print_exc()
    
    # 上传到 OSS
    print("\n" + "=" * 60)
    print("上传到 OSS...")
    print("=" * 60)
    
    oss_urls = []
    for angle_name, image_path in rendered_images:
        try:
            url = await upload_to_oss(image_path, angle_name)
            oss_urls.append((angle_name, url))
            print(f"✓ {angle_name}: {url}")
        except Exception as e:
            print(f"✗ {angle_name} 上传失败: {e}")
    
    # 打印汇总
    print("\n" + "=" * 60)
    print("渲染完成！OSS URL 列表：")
    print("=" * 60)
    for angle_name, url in oss_urls:
        print(f"{angle_name}: {url}")
    
    print(f"\n本地文件位置: {OUTPUT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
