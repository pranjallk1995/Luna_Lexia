FROM python:3.12-slim

WORKDIR /workspace/mcp

# Docling's OpenCV and ONNX dependencies need these shared libraries.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# Bake Docling's layout, table, and OCR models into the image so the first
# upload does not wait on downloads.
RUN docling-tools models download layout tableformer rapidocr

COPY app ./app
COPY tests ./tests

EXPOSE 8000

CMD ["python", "-m", "app.main"]
