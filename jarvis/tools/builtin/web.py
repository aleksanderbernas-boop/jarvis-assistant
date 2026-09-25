"""Web intelligence: search, read pages, open URLs, live weather."""

from __future__ import annotations

from pydantic import BaseModel, Field

from jarvis.tools.base import RetryPolicy, Tool, ToolContext, ToolError, ToolResult, TransientToolError
from jarvis.web.client import WebError
from jarvis.web.weather import Location


class SearchInput(BaseModel):
    query: str = Field(min_length=2, max_length=300)
    max_results: int = Field(6, ge=1, le=10)


class WebSearch(Tool):
    name = "web_search"
    description = "Search the web for current information. Returns titles, URLs and snippets."
    display_name = "Searching the web"
    category = "web"
    permission = "web"
    data_boundary = "remote"
    Input = SearchInput
    retry = RetryPolicy(max_attempts=2, backoff_s=1.0)
    parallel_safe = True

    async def execute(self, args: SearchInput, ctx: ToolContext) -> ToolResult:
        try:
            results = await ctx.services.web.search(args.query, args.max_results)
        except WebError as exc:
            raise TransientToolError(str(exc), kind="search_failed") from exc
        if not results:
            return ToolResult.success(f"No results for '{args.query}'", {"results": []})
        return ToolResult.success(f"Found {len(results)} results for '{args.query}'",
                                  {"results": [r.to_dict() for r in results]}, simulated=ctx.simulated)


class FetchInput(BaseModel):
    url: str
    max_chars: int = Field(8000, ge=500, le=30000)


class WebFetch(Tool):
    name = "web_fetch"
    description = "Open a web page and extract its readable text (to read an article, docs, a product page)."
    display_name = "Reading web page"
    category = "web"
    permission = "web"
    data_boundary = "remote"
    Input = FetchInput
    retry = RetryPolicy(max_attempts=2, backoff_s=1.0)
    parallel_safe = True

    async def execute(self, args: FetchInput, ctx: ToolContext) -> ToolResult:
        try:
            page = await ctx.services.web.fetch(args.url, args.max_chars)
        except WebError as exc:
            raise ToolError(str(exc), kind="fetch_failed") from exc
        return ToolResult.success(f"Read '{page.title}' ({len(page.text)} characters)",
                                  {"url": page.url, "title": page.title, "text": page.text}, simulated=ctx.simulated)


class OpenUrlInput(BaseModel):
    url: str = Field(description="Full URL, or a search phrase to open as a web search")


class OpenUrl(Tool):
    name = "open_url"
    description = "Open a URL (or a web search) in the user's browser so they can see it."
    display_name = "Opening in browser"
    category = "web"
    Input = OpenUrlInput

    async def execute(self, args: OpenUrlInput, ctx: ToolContext) -> ToolResult:
        from urllib.parse import quote_plus, urlparse

        url = args.url.strip()
        if not urlparse(url).scheme:
            url = f"https://{url}" if "." in url and " " not in url else f"https://duckduckgo.com/?q={quote_plus(url)}"
        if urlparse(url).scheme not in {"http", "https"}:
            raise ToolError("only http(s) links can be opened", kind="invalid_arguments")
        await ctx.platform.call(ctx.platform.open_url, url)
        return ToolResult.success(f"Opened {url} in the browser", {"url": url}, simulated=ctx.platform.simulated)


class WeatherInput(BaseModel):
    location: str | None = Field(None, description="Place name; omit for the user's configured location")
    days: int = Field(4, ge=1, le=7)


class Weather(Tool):
    name = "weather"
    description = "Live current weather and forecast (temperature, conditions, chance of rain)."
    display_name = "Checking the weather"
    category = "web"
    permission = "web"
    data_boundary = "remote"
    Input = WeatherInput
    retry = RetryPolicy(max_attempts=2, backoff_s=1.0)
    parallel_safe = True

    async def execute(self, args: WeatherInput, ctx: ToolContext) -> ToolResult:
        service = ctx.services.weather
        user = ctx.settings.user
        try:
            if args.location:
                location = await service.geocode(args.location)
            elif user.latitude is not None and user.longitude is not None:
                location = Location(user.location_name or "your location", user.latitude, user.longitude)
            else:
                raise ToolError("no location given and JARVIS_LATITUDE/JARVIS_LONGITUDE aren't configured",
                                kind="invalid_arguments")
            forecast = await service.forecast(location, args.days)
        except WebError as exc:
            raise TransientToolError(str(exc)) from exc
        current = forecast["current"]
        summary = f"{forecast['location']}: {current['temperature_c']}°C, {current['condition']}"
        return ToolResult.success(summary, forecast, simulated=bool(forecast.get("simulated")))


TOOLS = [WebSearch, WebFetch, OpenUrl, Weather]
