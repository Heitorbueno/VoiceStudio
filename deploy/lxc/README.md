# VoiceStudio em LXC (Proxmox) com domínio

## Container
- Template: Debian 12 ou Ubuntu 24.04, **x86_64**
- CPU-only: 4+ vCPU, 8–16 GB RAM, **40 GB+ disco** (venv ≈ 8 GB + modelos 2–5 GB cada). Unprivileged funciona.
- GPU NVIDIA: driver no host + passthrough de `/dev/nvidia*` no LXC (mesma versão de driver dentro do container), depois `GPU=nvidia`.

## Instalação
```bash
DOMAIN=voz.seudominio.com.br bash deploy/lxc/install.sh
```
- DNS `A` do domínio → IP público; portas 80/443 encaminhadas ao LXC (Caddy emite o certificado).
- Proxy fora do LXC (Nginx Proxy Manager, Traefik, Cloudflare Tunnel): `SKIP_CADDY=1`, aponte para `http://IP_DO_LXC:3900`
  e acrescente `FORWARDED_ALLOW_IPS=<IP exato do proxy>` em `/etc/voicestudio.env`.

## Acesso
Login no navegador com `OMNIVOICE_API_KEY` de `/etc/voicestudio.env`. Na primeira geração, aceite a licença do modelo
no Model Catalogue (download ≈ 2,4 GB do Hugging Face).

## Operação
```bash
systemctl status voicestudio ; journalctl -u voicestudio -f
# atualizar: rode o install.sh de novo (git reset + rebuild + restart)
systemctl restart voicestudio
```
Dados (SQLite, vozes, modelos): `/var/lib/voicestudio`. Migrações Alembic rodam sozinhas no start, com backup prévio do banco.
