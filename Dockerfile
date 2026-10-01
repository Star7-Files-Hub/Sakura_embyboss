# 第一阶段：构建阶段
FROM python:3.10.11-alpine AS builder

# 安装必要的构建依赖
RUN apk add --no-cache --virtual .build-deps gcc musl-dev openssl-dev coreutils
COPY requirements.txt .
# 安装 Python 包
RUN pip install --no-cache-dir -r requirements.txt
# 删除编译后的字节码文件
RUN find . -type f -name "*.pyc" -delete
# 清理构建依赖
RUN apk del --purge .build-deps
# 清理临时文件
RUN rm -rf /tmp/* /root/.cache /var/cache/apk/*

# 第二阶段：运行阶段
FROM python:3.10.11-alpine

# 设置环境变量
ENV TZ=Asia/Shanghai \
    DOCKER_MODE=1 \
    PYTHONUNBUFFERED=1 \
    WORKDIR=/app
# 安装必要的包
RUN apk add --no-cache \
    mariadb-connector-c \
    tzdata \
    mysql-client \
    git && \
    ln -snf Asia/Shanghai /etc/localtime && echo Asia/Shanghai > /etc/timezone

# 设置默认工作目录
WORKDIR ${WORKDIR}

# 复制构建阶段的输出
COPY --from=builder /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# 复制本地项目代码
# 密钥（config.json）、会话（*.session）、日志（log/）等敏感内容由仓库根目录的
# .dockerignore 排除，不再进入镜像层（D-H1）。
COPY . .

# 说明（D-L6）：旧版本此处是 `RUN find ./image -type f ! -name "bot2.png" -delete`。
# 那些文件由上一层 COPY 写入，-delete 只会生成 whiteout 层、并不能减小镜像体积，
# 因此已改为在 .dockerignore 中直接排除，这里不再重复清理。

# 非 root 用户运行（D-M1）。
# ⚠️ 部署提示：镜像以 uid/gid 1000 运行，挂载进容器的 config.json / log / db_backup
# 必须对该 uid 可读写（bot 启动时会 save_config() 回写 config.json）。
# 首次升级请执行：chown 1000:1000 config.json && chown -R 1000:1000 log db_backup
RUN addgroup -g 1000 -S app && \
    adduser -u 1000 -S -D -H -G app -s /sbin/nologin app && \
    mkdir -p /app/log /app/db_backup && \
    chown -R app:app /app
USER app

# 健康检查（D-M1）：确认 PID 1 仍是 bot 主进程（python3 main.py）。
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
    CMD grep -qa "main.py" /proc/1/cmdline || exit 1

# 设置启动命令
ENTRYPOINT [ "python3" ]
CMD [ "main.py" ]
