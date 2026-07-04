FROM mcr.microsoft.com/playwright/python:v1.60.0-noble

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PORT=8080

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

CMD ["sh", "-c", "Xvfb :99 -screen 0 1280x1024x24 -ac -nolisten tcp & sleep 2 && export DISPLAY=:99 && python -u run_all.py"]