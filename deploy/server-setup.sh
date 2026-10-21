#!/usr/bin/env bash
# 服务器初始化脚本（Ubuntu 22.04 / Debian 12 上测过；CentOS 系只做部分兼容）
#
# 做的事情，按顺序：
#   1. 装 Docker + compose 插件
#   2. 配镜像加速器 + 日志轮转（不配的话 docker.io 拉不动、JSON 日志能把磁盘写满）
#   3. 加 2GB swap（2C4G 机器加载 reranker 时会摸到内存上限）
#   4. 开本机防火墙的 22 / 80 / 8080
#
# 幂等：重复执行不会重复加 swap、不会重复写 daemon.json（会覆盖成同一份内容）
#
# 用法（在服务器上，root）：
#   bash deploy/server-setup.sh
#
# 注意：云厂商的「安全组 / 防火墙」是两层，本脚本只能管操作系统这一层，
#       控制台那一层必须自己去点（腾讯云轻量：防火墙 → 添加规则 → TCP:80）。
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "[x] 请用 root 运行：sudo bash $0"
    exit 1
fi

echo "===== 1/4 安装 Docker ====="
if command -v docker >/dev/null 2>&1; then
    echo "[=] 已安装：$(docker --version)"
else
    # --mirror Aliyun：官方脚本默认从 download.docker.com 装，国内经常超时
    curl -fsSL https://get.docker.com | bash -s docker --mirror Aliyun
fi

if docker compose version >/dev/null 2>&1; then
    echo "[=] compose 可用：$(docker compose version --short)"
else
    echo "[x] 没找到 docker compose 插件。Ubuntu 上执行：apt-get install -y docker-compose-plugin"
    exit 1
fi
systemctl enable --now docker

echo "===== 2/4 配置镜像加速与日志轮转 ====="
mkdir -p /etc/docker
# registry-mirrors：
#   mirror.ccs.tencentyun.com 只有腾讯云内网能用（零流量费、最快）
#   docker.m.daocloud.io 是公共镜像，其它云可用
# 阿里云用户：控制台「容器镜像服务 → 镜像加速器」会给一个专属地址，加在最前面最快
#
# log-opts：不加这个，应用 JSON 日志会无限增长，几个月后磁盘满、服务全挂。
#   50MB × 3 个文件 = 单容器最多 150MB
cat > /etc/docker/daemon.json <<'JSON'
{
  "registry-mirrors": [
    "https://mirror.ccs.tencentyun.com",
    "https://docker.m.daocloud.io"
  ],
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "50m",
    "max-file": "3"
  },
  "live-restore": true
}
JSON
systemctl restart docker

echo "===== 3/4 检查 swap ====="
if swapon --show | grep -q .; then
    echo "[=] 已有 swap：$(swapon --show=NAME,SIZE --noheadings | tr '\n' ' ')"
else
    echo "[+] 没有 swap，加 2GB"
    # fallocate 在部分文件系统（如某些云盘的 ext4）上会失败，用 dd 兜底
    fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    # swappiness=10：只在真的快没内存时才用 swap，避免正常运行时频繁换页拖慢响应
    sysctl -w vm.swappiness=10 >/dev/null
    grep -q '^vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=10' >> /etc/sysctl.conf
    echo "[+] swap 已启用：$(swapon --show=SIZE --noheadings | head -1)"
fi
free -h

echo "===== 4/4 配置本机防火墙 ====="
if command -v ufw >/dev/null 2>&1; then
    ufw allow 22/tcp >/dev/null
    ufw allow 80/tcp >/dev/null
    ufw allow 8080/tcp >/dev/null
    ufw --force enable >/dev/null
    echo "[=] ufw：$(ufw status | head -1)"
elif command -v firewall-cmd >/dev/null 2>&1; then
    for p in 22 80 8080; do firewall-cmd --permanent --add-port=${p}/tcp >/dev/null; done
    firewall-cmd --reload >/dev/null
    echo "[=] firewalld 已放行 22/80/8080"
else
    echo "[!] 没装 ufw / firewalld，跳过。云厂商安全组记得单独放行"
fi

echo
echo "===== 完成 ====="
echo "下一步："
echo "  1) git clone <你的仓库> && cd saojingling-agent"
echo "  2) 准备 .env（见 deploy/DEPLOY.md 第 3 步）"
echo "  3) PRELOAD_MODEL=0 docker compose build app"
echo "  4) docker compose up -d"
