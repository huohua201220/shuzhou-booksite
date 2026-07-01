FROM python:3.11-slim

WORKDIR /app

COPY app ./app

ENV HOST=0.0.0.0
ENV PORT=8765
ENV DATA_DIR=/data
ENV SITE_NAME=书舟
ENV SITE_SUBTITLE=手机上传书，阅读器浏览器直接取书
ENV MAX_UPLOAD_MB=200

VOLUME ["/data"]

EXPOSE 8765

CMD ["python", "app/server.py"]
