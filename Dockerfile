FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 依赖随镜像构建完成安装，启动即可用
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 构建期初始化自检：应用（含 /handoff-route、/inventory-allocation、
# /receipt-write-off、/waitlist-seats 等全部路由）可正常导入，tzdata 时区库可用；
# 失败则镜像构建失败，避免带病镜像启动
RUN python -c "from app.main import app; \
paths = {getattr(r, 'path', '') for r in app.routes}; \
assert '/handoff-route' in paths and '/inventory-allocation' in paths; \
assert '/receipt-write-off' in paths and '/waitlist-seats' in paths; \
from zoneinfo import ZoneInfo; ZoneInfo('UTC')"

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
