# Copyright (C) 2025 Alexander Vanhee, tfuxu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""
The composite as a GSK render-node tree.

`ImageProcessor` builds the same picture with Pillow on the CPU, which costs
tens of milliseconds per pass and has to be redone in full whenever any setting
moves. Here the source image is uploaded to the GPU once and every setting
becomes a property of a node tree that is rebuilt in tens of *micro*seconds, so
padding, corner radius and shadow stop costing anything to change.

The geometry below deliberately mirrors `ImageProcessor` formula for formula --
`geometry_parity_harness.py` asserts the two agree, so a change to one has to be
made to the other.

Two things are not GSK's job:

* **Gradients still come from `libgradient_gen.so`.** `Gsk.LinearGradientNode`
  disagrees with that library badly enough to change every saved preset (mean
  delta 19-25/255, ~78% of pixels off by more than 8), so the C library renders
  into a texture that is cached and reused. That texture depends on the canvas
  *aspect*, not just its size -- the library's output is scale-invariant under
  uniform scaling but not across aspect changes.
* **Downloading a rendered texture** is only needed for export.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import gi
from gi.repository import Gdk, GdkPixbuf, GLib, Gsk, Graphene, GObject

from gradia.graphics.background import Background
from gradia.graphics.gradient import GradientBackground
from gradia.graphics.image import ImageBackground
from gradia.graphics.loaded_image import BalancedPadding
from gradia.graphics.solid import SolidBackground
from gradia.utils.colors import hex_to_rgb

# The offset ImageProcessor hard-codes for the drop shadow, in preview pixels.
SHADOW_OFFSET = (10, 10)

# A GSK shadow's blur radius turns out to already be in the same units as the
# radius PIL's GaussianBlur takes: sweeping a conversion factor from 1.0 to 3.0
# in shadow_calibration_probe.py made the match monotonically worse, so there is
# no factor here. The residual difference is the falloff *shape* -- GSK and PIL
# blur a hard silhouette with different kernels -- and it stays under 8/255 mean
# inside the shadow band at mid strength.

# The gradient texture is regenerated when the canvas aspect moves by more than
# this, which is far below what is visible but coarse enough that a slider drag
# does not regenerate on every single tick.
ASPECT_CACHE_TOLERANCE = 0.01


def _rgba(r: float, g: float, b: float, a: float = 1.0) -> Gdk.RGBA:
    # Gdk.RGBA and the Gsk structs are plain C structs: PyGObject builds them
    # empty and takes field assignment, never keyword or positional arguments.
    colour = Gdk.RGBA()
    colour.red, colour.green, colour.blue, colour.alpha = r, g, b, a
    return colour


def _rect(x: float, y: float, w: float, h: float) -> Graphene.Rect:
    return Graphene.Rect().init(x, y, w, h)


@dataclass(frozen=True)
class CompositeGeometry:
    """
    Where everything lands, in canvas pixels.

    Mirrors ImageProcessor's `_calculate_final_dimensions`, `_get_paste_position`,
    `_apply_rounded_corners`, `_crop_image` and `_apply_auto_balance`.
    """
    canvas_width: int
    canvas_height: int
    image_x: int
    image_y: int
    image_width: int
    image_height: int
    corner_radius_px: float
    # Source-space crop, for negative padding. None means the whole source.
    crop: Optional[tuple[int, int, int, int]]
    # Auto balance grows the image rect and fills the extra with a flat colour;
    # this is the source's offset inside that rect, and the colour behind it.
    balance_offset: tuple[int, int] = (0, 0)
    balance_color: Optional[tuple[int, int, int, int]] = None

    @property
    def image_rect(self) -> Graphene.Rect:
        return _rect(self.image_x, self.image_y, self.image_width, self.image_height)

    @property
    def canvas_rect(self) -> Graphene.Rect:
        return _rect(0, 0, self.canvas_width, self.canvas_height)


def _parse_aspect_ratio(aspect_ratio) -> Optional[float]:
    if aspect_ratio is None:
        return None
    if isinstance(aspect_ratio, str) and ":" in aspect_ratio:
        try:
            w, h = map(float, aspect_ratio.split(":"))
            return w / h
        except Exception:
            return None
    try:
        return float(aspect_ratio)
    except Exception:
        return None


def compute_geometry(
    source_width: int,
    source_height: int,
    padding: int = 0,
    corner_radius: int = 0,
    aspect_ratio=None,
    rotation: int = 0,
    balanced_padding: Optional[BalancedPadding] = None,
    auto_balance: bool = False,
    scale: float = 1.0,
) -> CompositeGeometry:
    """
    Resolve the settings into rectangles.

    `scale` maps preview pixels onto full-resolution ones for export; every
    formula below is written in preview terms and multiplied through.
    """
    width, height = source_width, source_height

    # Rotation swaps the axes before anything else measures them.
    if rotation in (90, 270):
        width, height = height, width

    if auto_balance and balanced_padding is not None:
        left = int(balanced_padding.left * scale)
        right = int(balanced_padding.right * scale)
        top = int(balanced_padding.top * scale)
        bottom = int(balanced_padding.bottom * scale)
        width += left + right
        height += top + bottom

    balance_offset = (0, 0)
    balance_color = None
    if auto_balance and balanced_padding is not None:
        balance_offset = (int(balanced_padding.left * scale),
                          int(balanced_padding.top * scale))
        balance_color = balanced_padding.color

    crop = None
    if padding < 0:
        smaller = min(width, height)
        inset = int((abs(padding) / 100.0) * smaller)
        crop_w = max(1, width - 2 * inset)
        crop_h = max(1, height - 2 * inset)
        off_x = (width - crop_w) // 2
        off_y = (height - crop_h) // 2
        crop = (off_x, off_y, crop_w, crop_h)
        width, height = crop_w, crop_h

    radius_px = 0.0
    if corner_radius > 0:
        radius_px = (corner_radius / 100.0) * min(width, height)

    canvas_w, canvas_h = max(1, width), max(1, height)
    if padding >= 0:
        pad_px = int((padding / 100.0) * min(canvas_w, canvas_h))
        canvas_w += pad_px * 2
        canvas_h += pad_px * 2

    ratio = _parse_aspect_ratio(aspect_ratio)
    if ratio:
        current = canvas_w / canvas_h
        if current < ratio:
            canvas_w = int(canvas_h * ratio)
        elif current > ratio:
            canvas_h = int(canvas_w / ratio)

    if padding >= 0:
        x = (canvas_w - width) // 2
        y = (canvas_h - height) // 2
    else:
        x = y = 0

    return CompositeGeometry(
        canvas_width=canvas_w, canvas_height=canvas_h,
        image_x=x, image_y=y, image_width=width, image_height=height,
        corner_radius_px=radius_px, crop=crop,
        balance_offset=balance_offset, balance_color=balance_color,
    )


class GskCompositor:
    """
    Builds the composite as a node tree, caching what is expensive to rebuild.

    The source texture is uploaded once per image. The gradient texture is
    regenerated only when the gradient itself or the canvas aspect changes --
    corner radius and shadow strength never touch it, and neither does padding
    while the aspect stays put.
    """

    def __init__(self) -> None:
        self._source_texture: Optional[Gdk.Texture] = None
        self._source_key = None
        self._gradient_texture: Optional[Gdk.Texture] = None
        self._gradient_key = None
        self._background_texture: Optional[Gdk.Texture] = None
        self._background_key = None

    # -- textures -----------------------------------------------------------

    @staticmethod
    def _pil_to_texture(image) -> Gdk.Texture:
        if image.mode != "RGBA":
            image = image.convert("RGBA")
        data = GLib.Bytes.new(image.tobytes())
        return Gdk.MemoryTexture.new(
            image.width, image.height,
            Gdk.MemoryFormat.R8G8B8A8,
            data, image.width * 4,
        )

    def set_source_image(self, image, key=None) -> None:
        """Upload the source once; every later setting change reuses it."""
        if key is not None and key == self._source_key and self._source_texture:
            return
        self._source_texture = self._pil_to_texture(image)
        self._source_key = key

    def invalidate(self) -> None:
        self._source_texture = None
        self._source_key = None
        self._gradient_texture = None
        self._gradient_key = None
        self._background_texture = None
        self._background_key = None

    # -- background ---------------------------------------------------------

    def _background_node(self, background: Optional[Background],
                         geo: CompositeGeometry) -> Optional[Gsk.RenderNode]:
        rect = geo.canvas_rect
        if background is None:
            return None

        if isinstance(background, SolidBackground):
            r, g, b = hex_to_rgb(background.color)
            return Gsk.ColorNode.new(
                _rgba(r / 255, g / 255, b / 255, background.alpha), rect)

        if isinstance(background, GradientBackground):
            aspect = geo.canvas_width / max(1, geo.canvas_height)
            key = (background.to_json(),
                   round(aspect / ASPECT_CACHE_TOLERANCE))
            if key != self._gradient_key or self._gradient_texture is None:
                # Render at the canvas size the first time an aspect is seen;
                # a TextureNode then stretches it to any size with that aspect.
                image = background.prepare_image(geo.canvas_width, geo.canvas_height)
                self._gradient_texture = self._pil_to_texture(image)
                self._gradient_key = key
            return Gsk.TextureNode.new(self._gradient_texture, rect)

        if isinstance(background, ImageBackground):
            key = (background.file_path, geo.canvas_width, geo.canvas_height)
            if key != self._background_key or self._background_texture is None:
                image = background.prepare_image(geo.canvas_width, geo.canvas_height)
                if image is None:
                    return None
                self._background_texture = self._pil_to_texture(image)
                self._background_key = key
            return Gsk.TextureNode.new(self._background_texture, rect)

        # An unknown Background still renders, just through Pillow.
        image = background.prepare_image(geo.canvas_width, geo.canvas_height)
        return Gsk.TextureNode.new(self._pil_to_texture(image), rect) if image else None

    # -- the image itself ---------------------------------------------------

    def _image_node(self, geo: CompositeGeometry, rotation: int) -> Optional[Gsk.RenderNode]:
        """
        Place the source on the canvas: rotate about the centre, clip, round off.

        Everything is centred, which is what makes this simple -- Pillow's
        negative-padding crop is symmetric about the middle too, so clipping a
        centred full-size source to the (smaller) image rect gives exactly the
        same window without any crop arithmetic here.
        """
        if self._source_texture is None:
            return None

        rect = geo.image_rect
        tw = self._source_texture.get_width()
        th = self._source_texture.get_height()

        # Auto balance pads the source out to the image rect with a flat colour,
        # so the source stops being centred: it sits at the balance offset.
        centre_x = rect.origin.x + rect.size.width / 2
        centre_y = rect.origin.y + rect.size.height / 2
        if geo.balance_color is not None:
            off_x, off_y = geo.balance_offset
            centre_x = rect.origin.x + off_x + tw / 2
            centre_y = rect.origin.y + off_y + th / 2

        # Compose right-to-left: source centre to origin, rotate, then out to
        # wherever the image belongs on the canvas.
        transform = Gsk.Transform()
        transform = transform.translate(Graphene.Point().init(centre_x, centre_y))
        if rotation:
            # Gradia's rotation follows PIL's ROTATE_*, which turns
            # counter-clockwise; Gsk.Transform.rotate turns the other way.
            transform = transform.rotate(float(-rotation))
        transform = transform.translate(Graphene.Point().init(-tw / 2, -th / 2))

        node = Gsk.TransformNode.new(
            Gsk.TextureNode.new(self._source_texture, _rect(0, 0, tw, th)),
            transform)

        if geo.balance_color is not None:
            r, g, b, a = geo.balance_color
            fill = Gsk.ColorNode.new(_rgba(r / 255, g / 255, b / 255, a / 255), rect)
            node = Gsk.ContainerNode.new([fill, node])

        if geo.crop is not None:
            node = Gsk.ClipNode.new(node, rect)

        if geo.corner_radius_px > 0:
            rounded = Gsk.RoundedRect()
            rounded.init_from_rect(rect, geo.corner_radius_px)
            node = Gsk.RoundedClipNode.new(node, rounded)

        return node

    # -- the tree -----------------------------------------------------------

    def build(
        self,
        geo: CompositeGeometry,
        background: Optional[Background] = None,
        shadow_strength: float = 0.0,
        rotation: int = 0,
        scale: float = 1.0,
    ) -> Optional[Gsk.RenderNode]:
        image_node = self._image_node(geo, rotation)
        if image_node is None:
            return None

        if shadow_strength > 0:
            strength = max(0.0, min(shadow_strength, 10)) / 5
            shadow = Gsk.Shadow()
            shadow.color = _rgba(0, 0, 0, int(150 * strength) / 255)
            shadow.dx = SHADOW_OFFSET[0] * scale
            shadow.dy = SHADOW_OFFSET[1] * scale
            shadow.radius = 10 * strength * scale
            image_node = Gsk.ShadowNode.new(image_node, [shadow])

        background_node = self._background_node(background, geo)
        children = [n for n in (background_node, image_node) if n is not None]
        tree = Gsk.ContainerNode.new(children) if len(children) > 1 else children[0]

        # A shadow reaches outside the canvas; the canvas is what gets exported.
        return Gsk.ClipNode.new(tree, geo.canvas_rect)


class CompositePaintable(GObject.GObject, Gdk.Paintable):
    """
    Wraps a node tree so `Gtk.Picture` can show it.

    Slots in exactly where `Gdk.Texture.new_for_pixbuf()` used to, so the zoom
    controller and the drawing overlay keep reading intrinsic size the way they
    always have.
    """
    __gtype_name__ = "GradiaCompositePaintable"

    def __init__(self, node: Optional[Gsk.RenderNode] = None,
                 width: int = 1, height: int = 1) -> None:
        super().__init__()
        self._node = node
        self._width = max(1, width)
        self._height = max(1, height)

    def set_node(self, node: Optional[Gsk.RenderNode], width: int, height: int) -> None:
        size_changed = (width != self._width or height != self._height)
        self._node = node
        self._width = max(1, width)
        self._height = max(1, height)
        if size_changed:
            self.invalidate_size()
        self.invalidate_contents()

    @property
    def node(self) -> Optional[Gsk.RenderNode]:
        return self._node

    def do_get_intrinsic_width(self) -> int:
        return self._width

    def do_get_intrinsic_height(self) -> int:
        return self._height

    def do_get_intrinsic_aspect_ratio(self) -> float:
        return self._width / self._height if self._height else 0.0

    def do_snapshot(self, snapshot, width: float, height: float) -> None:
        if self._node is None:
            return
        snapshot.save()
        snapshot.scale(width / self._width, height / self._height)
        snapshot.append_node(self._node)
        snapshot.restore()
