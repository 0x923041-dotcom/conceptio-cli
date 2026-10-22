# Reproducible build for MCP registries that build from source (Glama's
# sandboxed Firecracker pipeline, one-click deploys). The stdio server starts
# keyless, so `tools/list` introspection works with no credentials; tool calls
# authenticate per-request via CONCEPTIO_API_KEY.
FROM python:3.12-slim

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .

CMD ["conceptio-search", "mcp"]
