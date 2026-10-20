# core/coords.py
from __future__ import annotations

from core import jsonc
import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

BASE_W = 1280
BASE_H = 720

COORD_FIELDS = ("roi", "target", "begin", "end")
RECOGNITION_TYPES = {"OCR", "TemplateMatch", "Or", "And", "FeatureMatch", "ColorMatch"}
NESTED_LISTS = ("any_of", "all_of", "swipes")


@dataclass
class DeviceProfile:
    name: str = "default"
    resolution: tuple[int, int] = (BASE_W, BASE_H)
    game_rect: tuple[int, int, int, int] = (0, 0, BASE_W, BASE_H)
    margin_left: int = 0
    margin_right: int = 0


@dataclass
class CoordEntry:
    value: list[int]
    align: str = "none"
    anchor: str = "none"


class CoordStore:
    def __init__(self, device_name: str, coords_dir: Path | None = None):
        if coords_dir is None:
            from core.config import COORDS_DIR
            coords_dir = COORDS_DIR
        self.coords_dir = coords_dir
        self.device_name = device_name
        self.profile = DeviceProfile()
        self.base_entries: dict[str, dict[str, CoordEntry]] = {}
        self.device_entries: dict[str, dict[str, CoordEntry]] = {}
        self.template_map: dict[str, str] = {}
        self.base_tolerance = 5
        self._load()

    def _load(self) -> None:
        base_path = self.coords_dir / "coords_base.yaml"
        if not base_path.is_file():
            raise FileNotFoundError(f"基准坐标文件不存在: {base_path}")
        with open(base_path, "r", encoding="utf-8") as f:
            base_raw = yaml.safe_load(f) or {}
        self.base_tolerance = int(base_raw.get("base_tolerance", 5))
        self.base_entries = self._parse_coords(base_raw.get("coords") or {})

        dev_path = self.coords_dir / f"coords_{self.device_name}.yaml"
        if dev_path.is_file():
            with open(dev_path, "r", encoding="utf-8") as f:
                dev_raw = yaml.safe_load(f) or {}
            self.profile = self._parse_profile(dev_raw)
            self.device_entries = self._parse_coords(dev_raw.get("coords") or {})
        else:
            logger.warning(f"设备坐标文件不存在: {dev_path}，按 16:9 无偏移处理")

    @staticmethod
    def _parse_coords(raw: dict) -> dict[str, dict[str, CoordEntry]]:
        out: dict[str, dict[str, CoordEntry]] = {}
        for node, fields in raw.items():
            if not isinstance(fields, dict):
                continue
            node_fields: dict[str, CoordEntry] = {}
            for field_name, spec in fields.items():
                if field_name not in COORD_FIELDS:
                    continue
                if isinstance(spec, list) and len(spec) >= 2:
                    node_fields[field_name] = CoordEntry(value=list(spec))
                elif isinstance(spec, dict):
                    value = spec.get("value")
                    if isinstance(value, list) and len(value) >= 2:
                        node_fields[field_name] = CoordEntry(
                            value=list(value),
                            align=str(spec.get("align", "none")),
                            anchor=str(spec.get("anchor", "none")),
                        )
            if node_fields:
                out[node] = node_fields
        return out

    @staticmethod
    def _parse_profile(dev_raw: dict) -> DeviceProfile:
        res = dev_raw.get("resolution") or [BASE_W, BASE_H]
        rect = dev_raw.get("game_rect") or [0, 0, int(res[0]), int(res[1])]
        margin = dev_raw.get("margin") or {}
        return DeviceProfile(
            name=str(dev_raw.get("profile", "unknown")),
            resolution=(int(res[0]), int(res[1])),
            game_rect=tuple(int(v) for v in rect),
            margin_left=int(margin.get("left", 0)),
            margin_right=int(margin.get("right", 0)),
        )

    # ---- 坐标变换 ----

    def _base_scale(self) -> float:
        w, h = self.profile.resolution
        return BASE_H / min(w, h)

    def _game_rect_in_base(self) -> tuple[float, float, float, float]:
        s = self._base_scale()
        gx, gy, gw, gh = self.profile.game_rect
        return gx * s, gy * s, gw * s, gh * s

    def transform_rect(self, rect: list[int], align: str = "none", anchor: str = "none") -> list[int]:
        if len(rect) < 2:
            return list(rect)
        gx_b, gy_b, gw_b, gh_b = self._game_rect_in_base()
        sx = gw_b / BASE_W
        sy = gh_b / BASE_H

        x = gx_b + rect[0] * sx
        if align == "left":
            x += self.profile.margin_left
        elif align == "right":
            x -= self.profile.margin_right

        if len(rect) < 4:
            if anchor == "top":
                y = gy_b + rect[1] * sx
            elif anchor == "bottom":
                y = gy_b + BASE_H - (BASE_H - rect[1]) * sx
            elif anchor == "center":
                y = gy_b + BASE_H / 2 + (rect[1] - BASE_H / 2) * sx
            else:
                y = gy_b + rect[1] * sy
            return [round(x), round(y)]

        y, w, h = rect[1], rect[2], rect[3]
        new_w = w * sx

        if anchor == "top":
            new_y = gy_b + y * sx
            new_h = h * sx
        elif anchor == "bottom":
            new_y = gy_b + BASE_H - (BASE_H - y) * sx
            new_h = h * sx
        elif anchor == "center":
            cy = y + h / 2
            new_cy = gy_b + BASE_H / 2 + (cy - BASE_H / 2) * sx
            new_y = new_cy - h / 2
            new_h = h
        else:  # none
            new_y = gy_b + y * sy
            new_h = h * sy

        return [round(x), round(new_y), round(new_w), round(new_h)]

    def _auto_align(self, roi: list[int]) -> str:
        center = roi[0] + roi[2] / 2
        if center < BASE_W * 0.45:
            return "left"
        elif center > BASE_W * 0.55:
            return "right"
        return "none"

    def _expand_roi(self, mapped: list[int], pad: int | None = None) -> list[int]:
        if len(mapped) < 4:
            return mapped
        if pad is None:
            pad = self.base_tolerance
        if pad <= 0:
            return mapped

        gx_b, gy_b, gw_b, gh_b = self._game_rect_in_base()
        scale = gw_b / BASE_W
        pad_px = round(pad * scale)
        x_min, x_max = round(gx_b), round(gx_b + gw_b)
        y_min, y_max = round(gy_b), round(gy_b + gh_b)
        x, y, w, h = mapped

        x2 = max(x_min, x - pad_px)
        w2 = min(x_max, x + w + pad_px) - x2
        y2 = max(y_min, y - pad_px)
        h2 = min(y_max, y + h + pad_px) - y2
        return [x2, y2, w2, h2]

    def _remap_template(self, t):
        if not self.template_map:
            return t
        if isinstance(t, list):
            return [self.template_map.get(x, x) for x in t]
        return self.template_map.get(t, t)

    # ---- override 构建 ----

    def build_override(self, pipeline_dir: Path) -> dict[str, dict]:
        override: dict[str, dict] = {}

        for json_file in sorted(pipeline_dir.glob("*.json")):
            try:
                with open(json_file, "r", encoding="utf-8") as f:
                    data = jsonc.load(f)
            except Exception as e:
                logger.warning(f"读取 {json_file} 失败: {e}")
                continue
            for node, spec in data.items():
                if not isinstance(spec, dict):
                    continue
                node_ov = self._collect_coord_fields(spec)
                if node_ov:
                    override[node] = node_ov

        # yaml 覆盖：放在 for json_file 循环外，只执行一次
        for node, fields in self._merged_entries().items():
            node_ov = override.setdefault(node, {})
            for field_name, entry in fields.items():
                node_ov[field_name] = self.transform_rect(entry.value, entry.align, entry.anchor)

        return override

    def _merged_entries(self) -> dict[str, dict[str, CoordEntry]]:
        merged: dict[str, dict[str, CoordEntry]] = {}
        for node, fields in self.base_entries.items():
            merged[node] = dict(fields)
        for node, fields in self.device_entries.items():
            merged.setdefault(node, {}).update(fields)
        return merged

    def _collect_coord_fields(self, spec: dict) -> dict:
        out: dict = {}
        is_reco = spec.get("recognition") in RECOGNITION_TYPES
        node_pad = spec.get("_pad")
        node_align = spec.get("_align")
        node_anchor = spec.get("_anchor") or "none"

        for field in COORD_FIELDS:
            value = spec.get(field)
            if not isinstance(value, list) or len(value) < 2:
                continue

            if field == "roi" and is_reco:
                align = node_align or self._auto_align(value)
                mapped = self.transform_rect(value, align, node_anchor)
                mapped = self._expand_roi(mapped, node_pad)
            else:
                mapped = self.transform_rect(value, "none", node_anchor)

            out[field] = mapped

        if "template" in spec:
            out["template"] = self._remap_template(spec["template"])

        for sub in NESTED_LISTS:
            children = spec.get(sub)
            if not isinstance(children, list):
                continue
            new_children = []
            changed = False
            for child in children:
                if not isinstance(child, dict):
                    new_children.append(child)
                    continue
                child_ov = self._collect_coord_fields(child)
                if child_ov:
                    new_children.append({**child, **child_ov})
                    changed = True
                else:
                    new_children.append(child)
            if changed:
                out[sub] = new_children

        return out


_store_cache: dict[str, CoordStore] = {}


def get_coord_store(device_name: str) -> CoordStore:
    if device_name not in _store_cache:
        _store_cache[device_name] = CoordStore(device_name)
    return _store_cache[device_name]