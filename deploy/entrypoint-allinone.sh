#!/bin/sh
# VisionDesk 单容器入口：PG 数据卷为空时初始化簇与账号（幂等），随后交给 supervisord
set -e

PGDATA=/var/lib/postgresql/data
PGBIN=/usr/lib/postgresql/17/bin

# 卷所有者可能来自旧栈容器（alpine postgres uid=999），统一改回本镜像 Debian postgres
chown -R postgres:postgres "$PGDATA" || true
mkdir -p /data

if [ ! -s "$PGDATA/PG_VERSION" ]; then
    echo "[entrypoint] 初始化全新 PostgreSQL 簇于 $PGDATA"
    su postgres -c "$PGBIN/initdb -D $PGDATA -E UTF8 --auth-local=trust --auth-host=scram-sha-256"
    # 临时拉起本地实例完成账号与建库
    su postgres -c "$PGBIN/pg_ctl -D $PGDATA -o '-c listen_addresses=127.0.0.1' -w start"
    su postgres -c "$PGBIN/psql -h 127.0.0.1 -c \"CREATE ROLE retouch LOGIN PASSWORD '${POSTGRES_PASSWORD:-retouch_dev}';\""
    su postgres -c "$PGBIN/createdb -h 127.0.0.1 -O retouch retouch"
    su postgres -c "$PGBIN/pg_ctl -D $PGDATA -w stop"
    echo "[entrypoint] PostgreSQL 初始化完成"
fi

exec /app/backend/.venv/bin/supervisord -n -c /etc/supervisor/supervisord.conf
