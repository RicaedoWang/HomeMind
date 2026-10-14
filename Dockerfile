# HomeMind 应用镜像
FROM python:3.11-slim

WORKDIR /app

# 依赖单独一层，利用 Docker 缓存：改代码不重装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir --disable-pip-version-check \
    -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

COPY src/ ./src/
COPY scripts/ ./scripts/

# src 布局：把 src 挂到模块搜索路径
ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1
ENV HM_ENV=production

EXPOSE 8000

CMD ["uvicorn", "homemind.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
