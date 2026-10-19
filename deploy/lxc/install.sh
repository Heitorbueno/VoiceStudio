#!/usr/bin/env bash
# VoiceStudio — instalação nativa em LXC (Debian 12 / Ubuntu 22.04+ x86_64)
# Uso (como root dentro do container):
#   DOMAIN=voz.seudominio.com.br bash deploy/lxc/install.sh
# Variáveis opcionais:
#   APP_DIR=/opt/voicestudio  DATA_DIR=/var/lib/voicestudio  PORT=3900
#   REPO_URL=https://github.com/Heitorbueno/VoiceStudio.git  BRANCH=main
#   GPU=nvidia   (instala a variante CUDA do torch; padrão usa o mesmo lock do Docker)
#   SKIP_CADDY=1 (se o proxy/TLS ficar fora do container: NPM, Traefik, Cloudflare Tunnel)
set -euo pipefail

: "${DOMAIN:?defina DOMAIN=voz.seudominio.com.br}"
APP_DIR="${APP_DIR:-/opt/voicestudio}"
DATA_DIR="${DATA_DIR:-/var/lib/voicestudio}"
PORT="${PORT:-3900}"
REPO_URL="${REPO_URL:-https://github.com/Heitorbueno/VoiceStudio.git}"
BRANCH="${BRANCH:-main}"
SVC_USER=voicestudio

[ "$(uname -m)" = "x86_64" ] || { echo "Somente x86_64 é suportado"; exit 1; }

echo "==> Pacotes do sistema"
apt-get update -qq
apt-get install -y -qq git curl ca-certificates build-essential ffmpeg libsndfile1 unzip python3

id "$SVC_USER" >/dev/null 2>&1 || useradd --system --create-home --home-dir /home/$SVC_USER --shell /usr/sbin/nologin "$SVC_USER"
mkdir -p "$APP_DIR" "$DATA_DIR"
chown -R "$SVC_USER:$SVC_USER" "$APP_DIR" "$DATA_DIR"

as_svc() { sudo -u "$SVC_USER" -H env PATH="/home/$SVC_USER/.local/bin:/home/$SVC_USER/.bun/bin:$PATH" bash -lc "$*"; }
command -v sudo >/dev/null || apt-get install -y -qq sudo

echo "==> Código"
if [ -d "$APP_DIR/.git" ]; then
  as_svc "cd '$APP_DIR' && git fetch --depth 1 origin '$BRANCH' && git reset --hard 'origin/$BRANCH'"
else
  as_svc "git clone --depth 1 -b '$BRANCH' '$REPO_URL' '$APP_DIR'"
fi

echo "==> uv + bun"
as_svc "command -v uv  >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh"
as_svc "command -v bun >/dev/null || curl -fsSL https://bun.sh/install | bash"

echo "==> Backend Python 3.11 (torch 2.8.0)"
as_svc "cd '$APP_DIR' && uv venv -p 3.11 .venv --allow-existing && \
  uv pip install --python .venv/bin/python --constraint deploy/torch-constraints.txt ."
if [ "${GPU:-}" = "nvidia" ]; then
  # Compat cuDNN 8 para CTranslate2 (WhisperX/faster-whisper), igual ao Dockerfile
  as_svc "cd '$APP_DIR' && T=\$(.venv/bin/python -c \"import os,sys;print(os.path.join(sys.prefix,'lib','python%d.%d'%sys.version_info[:2],'site-packages','cudnn8_compat'))\") && \
    uv pip install --python .venv/bin/python --no-deps --target \"\$T\" nvidia-cudnn-cu12==8.9.7.29"
fi

echo "==> Frontend web"
as_svc "cd '$APP_DIR' && bun install --frozen-lockfile && bun run --cwd electron build:web"

echo "==> Configuração"
BIND=127.0.0.1; [ -n "${SKIP_CADDY:-}" ] && BIND=0.0.0.0  # proxy externo precisa alcançar a porta
ENV_FILE=/etc/voicestudio.env
if [ ! -f "$ENV_FILE" ]; then
  KEY="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
  cat > "$ENV_FILE" <<EOF
OMNIVOICE_DATA_DIR=$DATA_DIR
HF_HOME=$DATA_DIR/huggingface
PYTHONPATH=$APP_DIR/backend
PYTHONUNBUFFERED=1
OMNIVOICE_SERVER_MODE=1
OMNIVOICE_BIND_HOST=$BIND
OMNIVOICE_PORT=$PORT
OMNIVOICE_API_KEY=$KEY
OMNIVOICE_ALLOWED_HOSTS=$DOMAIN
OMNIVOICE_ANALYTICS_DISABLED=1
# HF_TOKEN=hf_xxx   # opcional: modelos gated / downloads mais rápidos
EOF
  chmod 600 "$ENV_FILE"
  echo "    Chave de administrador gerada em $ENV_FILE (OMNIVOICE_API_KEY)"
fi

install -m 644 "$APP_DIR/deploy/lxc/voicestudio.service" /etc/systemd/system/voicestudio.service
sed -i "s#__APP_DIR__#$APP_DIR#g; s#__DATA_DIR__#$DATA_DIR#g" /etc/systemd/system/voicestudio.service
systemctl daemon-reload
systemctl enable --now voicestudio

if [ -z "${SKIP_CADDY:-}" ]; then
  echo "==> Caddy (HTTPS automático)"
  apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https gnupg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq && apt-get install -y -qq caddy
  sed "s#__DOMAIN__#$DOMAIN#g; s#__PORT__#$PORT#g" "$APP_DIR/deploy/lxc/Caddyfile" > /etc/caddy/Caddyfile
  systemctl reload caddy || systemctl restart caddy
fi

echo "==> Aguardando /health"
for _ in $(seq 1 60); do curl -sf "http://127.0.0.1:$PORT/health" && break; sleep 3; done
echo
echo "Pronto: https://$DOMAIN  (login com a OMNIVOICE_API_KEY de $ENV_FILE)"
