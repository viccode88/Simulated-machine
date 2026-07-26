# 所有模擬設備共用同一個映像，但每台設備仍是獨立容器、獨立行程、獨立 Modbus server。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    CONFIG_DIR=/app/configs \
    STATE_DIR=/var/lib/plant-device

WORKDIR /app

# pymodbus 版本必須固定：其文件明確提醒版本不完全遵循 SemVer
RUN pip install --no-cache-dir \
        "pymodbus==3.14.0" \
        "PyYAML==6.0.2" \
        "aiohttp==3.10.11"

COPY common ./common
COPY devices ./devices
COPY controller ./controller
COPY plant_bus ./plant_bus
COPY historian ./historian
COPY hmi ./hmi
COPY tools ./tools
COPY configs ./configs

RUN mkdir -p /var/lib/plant-device /var/lib/plant-bus

CMD ["python", "-m", "plant_bus.app.main"]
