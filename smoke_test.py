import asyncio
import os

from mcp import ClientSession
from mcp.client.sse import sse_client

SSE_URL = os.environ.get("SSE_URL", "http://localhost:7860/sse")
TOKEN = os.environ["TEST_TOKEN"]  # JWT with weatherfusion:read scope


async def main():
    async with sse_client(SSE_URL, headers={"Authorization": f"Bearer {TOKEN}"}) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            assert len(tools) == 5, f"Expected 5 tools, got {len(tools)}"
            for t in tools:
                assert "weatherfusion:read" in t.description

            result = await session.call_tool("get_current_weather", {"lat": 28.6139, "lon": 77.209})
            assert result.content
            print("PASS: get_current_weather returned data")

            try:
                await session.call_tool("get_current_weather", {"lat": 999, "lon": 999})
            except Exception:
                print("PASS: invalid coords propagated error correctly")


if __name__ == "__main__":
    asyncio.run(main())
