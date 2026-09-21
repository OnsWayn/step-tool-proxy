FROM python:3.12-slim

WORKDIR /app
COPY step_tool_proxy/ ./step_tool_proxy/
COPY static/ ./static/
COPY requirements.txt ./

ENV PROXY_HOST=0.0.0.0
ENV PROXY_PORT=8722
ENV DATA_DIR=/app/data
ENV FORCE_BUFFER=1

RUN mkdir -p /app/data
VOLUME ["/app/data"]
EXPOSE 8722

CMD ["python", "-m", "step_tool_proxy"]
