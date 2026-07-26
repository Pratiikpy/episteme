"""Document pack node: document.to_markdown (text + PDF via pypdf)."""
from __future__ import annotations

import base64
import io

from contract import ErrorCode, ValidationCheck
from runtime import Node, NodeContext, NodeError


class DocumentToMarkdownNode(Node):
    name = "document.to_markdown"
    price_usdt = 0.02
    deterministic = True
    engine = "pypdf+episteme"

    def engine_available(self) -> bool:
        return True  # text path always; pdf path checks pypdf at runtime

    def run(self, ctx: NodeContext) -> dict:
        inp = ctx.input
        if "text" in inp and "content_b64" not in inp:
            text = str(inp["text"])
            return {"markdown": text.strip(), "pages": 1, "source_format": "text",
                    "char_count": len(text.strip())}
        if "content_b64" not in inp:
            raise NodeError(ErrorCode.INVALID_INPUT, "provide 'text' or 'content_b64'")
        data = base64.b64decode(inp["content_b64"])
        if data[:5] == b"%PDF-":
            try:
                import pypdf
            except Exception:
                raise NodeError(ErrorCode.ENGINE_UNAVAILABLE, "pypdf not installed")
            try:
                reader = pypdf.PdfReader(io.BytesIO(data))
                parts = []
                for i, page in enumerate(reader.pages):
                    txt = page.extract_text() or ""
                    parts.append(f"\n## Page {i+1}\n\n{txt.strip()}")
                md = "\n".join(parts).strip()
            except Exception as e:
                raise NodeError(ErrorCode.ENGINE_FAILED, f"pdf parse failed: {e}")
            return {"markdown": md, "pages": len(reader.pages),
                    "source_format": "pdf", "char_count": len(md)}
        # fallback: try utf-8 text
        try:
            text = data.decode("utf-8")
            return {"markdown": text.strip(), "pages": 1, "source_format": "text",
                    "char_count": len(text.strip())}
        except UnicodeDecodeError:
            raise NodeError(ErrorCode.UNSUPPORTED_FORMAT, "unsupported binary format for markdown")

    def validate(self, result: dict, ctx: NodeContext) -> list[ValidationCheck]:
        return [
            ValidationCheck(name="pages_positive", passed=result.get("pages", 0) >= 1),
            ValidationCheck(name="has_markdown_key", passed="markdown" in result),
        ]
