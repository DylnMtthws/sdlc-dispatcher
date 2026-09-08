# Trusted browser harness. Browser runs only on the preview's internal network.
FROM mcr.microsoft.com/playwright/python:v1.60.0-noble
RUN pip install --no-cache-dir playwright==1.60.0
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65532:65532
