# VoiceStudio em LXC + Cloudflare Tunnel

Endereço: **https://voicestudio.liderautomacoes.com.br** — single-tenant, uma chave de administrador.

## 1. Container (Proxmox)
- Debian 12 ou Ubuntu 24.04, **x86_64**, unprivileged
- 4+ vCPU, 8–16 GB RAM, **40 GB+ de disco** (venv ≈ 8 GB + modelos 2–5 GB cada)
- Nenhuma porta precisa ser aberta no roteador: o túnel é só de saída
- GPU NVIDIA (opcional): passthrough de `/dev/nvidia*` com o mesmo driver do host, e `GPU=nvidia` na instalação

## 2. Túnel na Cloudflare
1. Zero Trust → **Networks → Tunnels → Create a tunnel** → tipo *Cloudflared* → nome `voicestudio`
2. Copie o **token** exibido no comando de instalação (`eyJ...`)
3. Aba **Public Hostname**: subdomínio `voicestudio`, domínio `liderautomacoes.com.br`,
   Service **HTTP** → `127.0.0.1:3900`. O DNS (CNAME) é criado automaticamente.

## 3. Instalação (root, dentro da LXC)
```bash
apt-get update && apt-get install -y git
git clone https://github.com/Heitorbueno/VoiceStudio.git /root/voicestudio-src
CF_TUNNEL_TOKEN='eyJ...' bash /root/voicestudio-src/deploy/lxc/install.sh
```
Instala backend (Python 3.11 + torch), builda a UI, cria o serviço `voicestudio` e o `cloudflared`.
A chave de acesso fica em `/etc/voicestudio.env` (`OMNIVOICE_API_KEY`) — `grep API_KEY /etc/voicestudio.env`.

## 4. Primeiro acesso
Abra o domínio, entre com a chave e, na primeira geração, aceite a licença do modelo no
Model Catalogue (download ≈ 2,4 GB do Hugging Face).

## Conector Claude / ChatGPT (MCP)
1. No VoiceStudio: **Configurações → Compartilhamento → Conector Claude / ChatGPT** → copie a URL
   (`https://voicestudio.liderautomacoes.com.br/mcp`).
2. Claude: *Configurações → Conectores → Adicionar conector personalizado* e cole a URL.
   ChatGPT: *Configurações → Conectores* (modo desenvolvedor) → *Criar* e cole a URL.
3. O navegador abre o VoiceStudio: entre com a chave, confira o cliente e clique **Permitir**.

O conector usa OAuth (PKCE + aprovação sua a cada conexão); a chave de administrador nunca sai da LXC.
O token do conector só vale em `/mcp` e não acessa a API de administração. Para cortar o acesso,
revogue a conexão no mesmo cartão. O instalador grava `OMNIVOICE_PUBLIC_URL` e
`OMNIVOICE_MCP_ALLOWED_HOSTS` no `/etc/voicestudio.env` (também em instalações antigas, ao atualizar).
Com Cloudflare Access ativo, crie *Bypass* para `/mcp`, `/oauth/*` e `/.well-known/*`.

## Segurança
- Backend escuta só em `127.0.0.1`; o `cloudflared` é o único caminho externo.
- Uvicorn confia em `X-Forwarded-For` só vindo de `127.0.0.1` e usa o IP mais à direita (o que a
  Cloudflare acrescenta): `X-Forwarded-For: 127.0.0.1` forjado por um cliente não dá acesso.
- Opcional, camada extra: Zero Trust → **Access → Applications** com login por e-mail no domínio.
  Se ativar, crie uma regra *Bypass* para `/v1/*`, `/mcp`, `/oauth/*` e `/.well-known/*` caso use a API/MCP fora do navegador.

## Limites da Cloudflare (plano Free)
- Upload máximo de **100 MB** por requisição (vídeos grandes para dublagem: use links/URL ou corte o arquivo)
- Resposta sem dados por mais de **100 s** retorna 524. Streaming, eventos e jobs longos (audiobook, dublagem)
  enviam heartbeat e não são afetados; uma geração única de texto muito longo **em CPU** pode passar de 100 s —
  divida o texto ou use GPU

## Operação
```bash
systemctl status voicestudio cloudflared
journalctl -u voicestudio -f
bash /opt/voicestudio/deploy/lxc/install.sh     # atualizar (git pull + rebuild + restart)
```
Dados (SQLite, vozes, saídas, modelos): `/var/lib/voicestudio`. As migrações Alembic rodam no start,
com backup automático do banco antes.

### Outros modos
- `PROXY=caddy` — HTTPS direto com Let's Encrypt (portas 80/443 encaminhadas à LXC)
- `PROXY=none` — proxy externo (NPM/Traefik): backend em `0.0.0.0:3900`; defina
  `FORWARDED_ALLOW_IPS=<IP exato do proxy>` em `/etc/voicestudio.env`
