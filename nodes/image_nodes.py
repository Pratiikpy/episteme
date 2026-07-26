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
        # `op` is declared REQUIRED in the schema, so defaulting it here was a silent lie: a caller
        # who sent {content_b64, width: 200} got a plain format-convert, their width was dropped
        # without a word, and the unchanged image was signed as a success. Same family as the
        # data.clean bug — asked for one thing, quietly did another, then attested to it.
        if "op" not in ctx.input or not str(ctx.input.get("op") or "").strip():
            raise NodeError(ErrorCode.INVALID_INPUT,
                            "op is required — one of resize, thumbnail, convert, grayscale, rotate")
        op = str(ctx.input["op"]).strip().lower()
        out_fmt = str(ctx.input.get("format", "PNG")).upper()
        if out_fmt not in {"PNG", "JPEG", "WEBP", "GIF", "BMP"}:
            raise NodeError(ErrorCode.INVALID_INPUT, f"unsupported output format {out_fmt}")

        if op in ("resize", "thumbnail"):
            req_w, req_h = ctx.input.get("width"), ctx.input.get("height")
            if req_w is None and req_h is None:
                raise NodeError(ErrorCode.INVALID_INPUT, f"{op} needs width and/or height")
            # Width-only (or height-only) must SCALE, not stretch. Defaulting the missing side to the
            # input's own dimension silently distorted the picture while reporting success.
            if req_w is not None and req_h is None:
                w = int(req_w)
                h = max(1, round(in_h * (w / in_w)))
            elif req_h is not None and req_w is None:
                h = int(req_h)
                w = max(1, round(in_w * (h / in_h)))
            else:
                w, h = int(req_w), int(req_h)
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
        checks = [
            ValidationCheck(name="produced_output", passed=result.get("out_bytes", 0) > 0),
            ValidationCheck(name="has_out_hash", passed=len(result.get("out_sha256", "")) == 64),
        ]
        # Assert the work, not its shape: if a size was requested, the output must actually be that
        # size, and a single-axis request must have preserved the aspect ratio rather than stretched.
        op = str(ctx.input.get("op") or "").strip().lower()
        if op == "resize":
            rw, rh = ctx.input.get("width"), ctx.input.get("height")
            ow, oh = result.get("out_width"), result.get("out_height")
            iw, ih = result.get("in_width"), result.get("in_height")
            if rw is not None and rh is None:
                checks.append(ValidationCheck(
                    name="resized_to_requested_width", passed=ow == int(rw),
                    detail=f"asked {rw}px wide, produced {ow}px"))
                expect_h = max(1, round(ih * (int(rw) / iw))) if iw else None
                checks.append(ValidationCheck(
                    name="aspect_ratio_preserved", passed=oh == expect_h,
                    detail=f"{iw}x{ih} -> {ow}x{oh}, expected height {expect_h}"))
            elif rh is not None and rw is None:
                checks.append(ValidationCheck(
                    name="resized_to_requested_height", passed=oh == int(rh),
                    detail=f"asked {rh}px tall, produced {oh}px"))
                expect_w = max(1, round(iw * (int(rh) / ih))) if ih else None
                checks.append(ValidationCheck(
                    name="aspect_ratio_preserved", passed=ow == expect_w,
                    detail=f"{iw}x{ih} -> {ow}x{oh}, expected width {expect_w}"))
            elif rw is not None and rh is not None:
                checks.append(ValidationCheck(
                    name="resized_to_requested_box", passed=(ow == int(rw) and oh == int(rh)),
                    detail=f"asked {rw}x{rh}, produced {ow}x{oh}"))
        return checks
