FROM python:3.12-slim

WORKDIR /workspace/mcp

# Docling's OpenCV and ONNX dependencies need these shared libraries.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# uv installs the requirements; its cache mount reuses downloads across rebuilds.
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /bin/uv
ENV UV_LINK_MODE=copy
# requirements.txt adds the PyTorch CUDA 13.0 index for torch; let uv pick the best
# version across both indexes, as pip did, since that index also carries old
# copies of common packages such as certifi.
ENV UV_INDEX_STRATEGY=unsafe-best-match

COPY requirements.txt .

RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system -r requirements.txt

# Bake Docling's layout, table, and OCR models into the image so the first upload
# does not wait on downloads; DoclingParser.ARTIFACTS_PATH loads them from here.
RUN docling-tools models download layout tableformer rapidocr

# Headless LibreOffice converts Word and Excel uploads to PDF (OfficeConverter);
# Carlito, Caladea, and Liberation match the metrics of Calibri, Cambria, Arial,
# and Times, so converted pages keep their original layout.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libreoffice-writer-nogui libreoffice-calc-nogui \
        fonts-crosextra-carlito fonts-crosextra-caladea fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

COPY app ./app
COPY tests ./tests

EXPOSE 8000

CMD ["python", "-m", "app.main"]
