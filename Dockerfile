FROM python:3.11-slim

# Hugging Face Spaces ใช้ port 7860 เป็น standard
EXPOSE 7860

WORKDIR /app

COPY requirements.txt .
RUN pip install --default-timeout=1000 --no-cache-dir -r requirements.txt
COPY . .

# Default: HuggingFace port (override ด้วย ENV PORT สำหรับ local)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]