"""Image pack nodes (Pillow): image.inspect, image.transform."""
from __future__ import annotations

import base64
import io

from contract import ErrorCode, ValidationCheck
from runtime import Node, NodeContext, NodeError


def _img_bytes(inp: dict) -> bytes:
    if "content_b64" not in inp:
        raise NodeError(ErrorCode.INVALID_INPUT, "provide image 'content_b64'")
    try:
        return base64.b64decode(inp["content_b64"])
    except Exception as e:
        raise NodeError(ErrorCode.INVALID_INPUT, f"invalid base64: {e}")


class ImageInspectNode(Node):
    name = "image.inspect"
    price_usdt = 0.002
    deterministic = True
    engine = "pillow"

    def engine_available(self) -> bool:
        try:
            import PIL  # noqa
            return True
        except Exception:
            return False

    def run(self, ctx: NodeContext) -> dict:
        from PIL import Image
        import hashlib
        data = _img_bytes(ctx.input)
        try:
            im = Image.open(io.BytesIO(data))
            im.load()
        except Exception as e:
            raise NodeError(ErrorCode.UNSUPPORTED_FORMAT, f"not an image: {e}")
        exif = {}
        try:
            raw = im.getexif()
            exif = {str(k): str(v)[:120] for k, v in raw.items()}
        except Exception:
            pass
        return {
            "format": im.format,
            "mode": im.mode,
            "width": im.width,
            "height": im.height,
            "megapixels": round(im.width * im.height / 1e6, 4),
            "has_exif": bool(exif),
            "exif": exif,
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        return [
            ValidationCheck(name="dims_positive", passed=result.get("width", 0) > 0 and result.get("height", 0) > 0),
            ValidationCheck(name="format_detected", passed=bool(result.get("format"))),
        ]


class ImageTransformNode(Node):
    name = "image.transform"
    price_usdt = 0.01
    deterministic = True
    engine = "pillow"

    def engine_available(self) -> bool:
        try:
            import PIL  # noqa
            return True
        except Exception:
            return False

    def run(self, ctx: NodeContext) -> dict:
        from PIL import Image
        import hashlib
        data = _img_bytes(ctx.input)
        try:
            im = Image.open(io.BytesIO(data))
            im.load()
        except Exception as e:
            raise NodeError(ErrorCode.UNSUPPORTED_FORMAT, f"not an image: {e}")
        in_w, in_h = im.width, im.height
        op = str(ctx.input.get("op", "convert"))
        out_fmt = str(ctx.input.get("format", "PNG")).upper()
        if out_fmt not in {"PNG", "JPEG", "WEBP", "GIF", "BMP"}:
            raise NodeError(ErrorCode.INVALID_INPUT, f"unsupported output format {out_fmt}")

        if op in ("resize", "thumbnail"):
            w = int(ctx.input.get("width", in_w))
            h = int(ctx.input.get("height", in_h))
            if w <= 0 or h <= 0 or w > 20000 or h > 20000:
                raise NodeError(ErrorCode.INVALID_INPUT, "bad target dimensions")
            if op == "thumbnail":
                im.thumbnail((w, h))
            else:
                im = im.resize((w, h))
        elif op == "convert":
            pass
        elif op == "grayscale":
            im = im.convert("L")
        elif op == "rotate":
            im = im.rotate(int(ctx.input.get("degrees", 90)), expand=True)
        else:
            raise NodeError(ErrorCode.INVALID_INPUT, f"unknown op '{op}'")

        if out_fmt == "JPEG" and im.mode in ("RGBA", "P", "LA"):
            im = im.convert("RGB")
        buf = io.BytesIO()
        save_kwargs = {"optimize": True} if out_fmt in ("PNG", "JPEG") else {}
        im.save(buf, format=out_fmt, **save_kwargs)
        out = buf.getvalue()
        ref = ctx.add_artifact(f"transformed.{out_fmt.lower()}", out,
                               f"image/{out_fmt.lower()}")
        return {
            "op": op,
            "in_width": in_w, "in_height": in_h,
            "out_width": im.width, "out_height": im.height,
            "out_format": out_fmt,
            "out_bytes": len(out),
            "out_sha256": hashlib.sha256(out).hexdigest(),
            "artifact_uri": ref.uri,
        }

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        return [
            ValidationCheck(name="produced_output", passed=result.get("out_bytes", 0) > 0),
            ValidationCheck(name="has_out_hash", passed=len(result.get("out_sha256", "")) == 64),
        ]
