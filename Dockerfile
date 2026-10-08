FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY requirements.txt pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir -e .

COPY configs ./configs
EXPOSE 8765
CMD ["uvicorn", "mongoose.app:app", "--host", "0.0.0.0", "--port", "8765"]
