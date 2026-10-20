# core/template_resizer.py
"""按设备图像空间缩放模板图片，让模板和 ROI 在同一空间。"""
from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp")


def resize_templates(
    src_dir: Path,
    dst_dir: Path,
    scale: float,
    overwrite: bool = False,
) -> dict[str, str]:
    """
    把 src_dir 下的所有模板按 scale 缩放到 dst_dir。
    返回 {原文件名: 缩放后绝对路径} 映射。
    scale = image_target_width / template_native_width
    """
    mapping: dict[str, str] = {}

    # 已存在且不强制覆盖时，直接复用
    if not overwrite and dst_dir.exists():
        for f in dst_dir.iterdir():
            if f.suffix.lower() in IMAGE_EXTS:
                mapping[f.name] = str(f)
        if mapping:
            logger.info(f"[templates] 复用已缓存: {dst_dir} ({len(mapping)} 个)")
            return mapping

    dst_dir.mkdir(parents=True, exist_ok=True)

    # 下采样用 LANCZOS（高质量抗锯齿），上采样用 BICUBIC（平滑）
    resample = Image.LANCZOS if scale < 1.0 else Image.BICUBIC

    for src in src_dir.iterdir():
        if src.suffix.lower() not in IMAGE_EXTS:
            continue
        try:
            img = Image.open(src)
            new_w = max(1, round(img.width * scale))
            new_h = max(1, round(img.height * scale))
            resized = img.resize((new_w, new_h), resample=resample)
            dst = dst_dir / src.name
            resized.save(dst)
            mapping[src.name] = str(dst)
        except Exception as e:
            logger.warning(f"[templates] 处理失败: {src}, error={e}")

    logger.info(
        f"[templates] 缩放完成: scale={scale:.4f}, "
        f"src={src_dir}, dst={dst_dir}, count={len(mapping)}"
    )
    return mapping