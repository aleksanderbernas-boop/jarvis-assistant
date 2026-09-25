"""Screenshots and (permission-gated) screen understanding with a vision model."""

from __future__ import annotations

import base64
import io
from datetime import datetime

from pydantic import BaseModel, Field

from jarvis.llm.base import LLMError
from jarvis.llm.models import ModelTier
from jarvis.tools.base import Tool, ToolContext, ToolError, ToolResult

MAX_VISION_BYTES = 3_500_000


def _prepare_for_vision(png: bytes) -> tuple[bytes, str]:
    """Downscale to keep uploads small and fast. Falls back to the raw PNG without Pillow."""
    try:
        from PIL import Image
    except ImportError:
        if len(png) > MAX_VISION_BYTES:
            raise ToolError("the screenshot is too large to send without Pillow: pip install pillow",
                            kind="not_supported") from None
        return png, "image/png"
    image = Image.open(io.BytesIO(png)).convert("RGB")
    image.thumbnail((1600, 1600))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=80)
    return out.getvalue(), "image/jpeg"


class ScreenshotInput(BaseModel):
    active_window_only: bool = False


class TakeScreenshot(Tool):
    name = "take_screenshot"
    description = "Capture the screen (or the active window) and save it locally as a PNG."
    display_name = "Capturing the screen"
    category = "screen"
    Input = ScreenshotInput

    async def execute(self, args: ScreenshotInput, ctx: ToolContext) -> ToolResult:
        png = await ctx.platform.call(ctx.platform.screenshot, args.active_window_only)
        folder = ctx.settings.data_dir / "screenshots"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"screenshot_{datetime.now():%Y-%m-%d_%H%M%S}.png"
        path.write_bytes(png)
        return ToolResult.success(f"Screenshot saved to {path}", {"path": str(path), "bytes": len(png)},
                                  verified=path.stat().st_size > 0, simulated=ctx.platform.simulated)


class DescribeScreenInput(BaseModel):
    question: str = Field("Describe what is on the screen.", description="What the user wants to know")
    active_window_only: bool = False


class DescribeScreen(Tool):
    name = "describe_screen"
    description = ("Take a screenshot and analyse it with the vision model: what's on screen, read visible text, "
                   "identify UI controls. Sends the image to the model provider.")
    display_name = "Analysing the screen"
    category = "screen"
    permission = "vision"
    data_boundary = "remote"
    Input = DescribeScreenInput
    timeout_s = 60.0

    async def execute(self, args: DescribeScreenInput, ctx: ToolContext) -> ToolResult:
        png = await ctx.platform.call(ctx.platform.screenshot, args.active_window_only)
        if ctx.platform.simulated:
            return ToolResult.success("Simulated screen: a desktop with a browser and a code editor open.",
                                      {"answer": "Simulated screen contents (mock mode)."}, simulated=True)
        image, mime = _prepare_for_vision(png)
        encoded = base64.b64encode(image).decode("ascii")
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": f"{args.question}\nBe specific and factual. Quote visible text exactly. "
                                         "If something is unreadable, say so."},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
            ],
        }]
        try:
            answer = await ctx.services.llm.complete(ctx.services.models.model_for(ModelTier.VISION), messages,
                                                     temperature=0.2, max_tokens=900)
        except LLMError as exc:
            raise ToolError(f"vision analysis failed: {exc.user_message()}", kind="llm_error") from exc
        return ToolResult.success("Screen analysed", {"answer": answer})


TOOLS = [TakeScreenshot, DescribeScreen]
