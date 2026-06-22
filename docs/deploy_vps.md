# Self-hosting the Echo Loop Web UI on a VPS

Run the generator as a small web app so you can paste text and get Echo Loop
audio from your phone — no laptop or Claude Code needed.

The UI exposes the two text-only modes:

- **文本 (Text):** paste bilingual lines `target|||native`.
- **面试稿 (Interview):** paste `Q:/A:` lines, each `target|||native`.

Audio is generated with **Google Cloud TTS** by default (edge-tts is a free
fallback you can pick per job). Output is `.m4a` + `.lrc`, playable and
downloadable directly in the mobile browser.

Recommended access path: **Tailscale** (private, no public exposure, no domain
or TLS needed).

---

## 1. Prerequisites on the VPS

- Docker + Docker Compose plugin
  ```bash
  curl -fsSL https://get.docker.com | sh
  ```
- A Google Cloud service-account JSON with the **Cloud Text-to-Speech User**
  role. (Same file the CLI uses — see the main README "Google Cloud TTS Setup".)

## 2. Get the code + credentials in place

```bash
git clone <your-repo-url> echoEnglish
cd echoEnglish

# Copy your Google credentials to the project root (gitignored).
# It is mounted read-only into the container at /secrets/google-credentials.json
cp /path/to/your-service-account.json ./google-credentials.json

# Optional config
cp .env.example .env      # then edit if you want a token / different port
```

`config.yaml` is mounted read-only into the container, so any voice / timing /
loop defaults you set there apply to the web UI too.

## 3. First start (pull the prebuilt image)

The image is built by CI and published to Docker Hub as
`sudami/echo_english:latest` (see [section 6](#6-auto-deploy-with-github-actions)).
The VPS just pulls and runs it — no building on the VPS:

```bash
docker compose pull
docker compose up -d
docker compose logs -f      # watch startup
```

> If the Docker Hub repo is **private**, run `docker login` once on the VPS
> first so `docker compose pull` is authenticated.

The app listens on port **8080**. Verify locally on the VPS:

```bash
curl http://localhost:8080/healthz   # -> {"ok": true}
```

## 4. Reach it from your phone via Tailscale

On the VPS:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
tailscale ip -4            # note the 100.x.y.z address
```

Install Tailscale on your phone, sign into the same tailnet, then open:

```
http://<vps-tailscale-ip>:8080
```

### Lock down the public interface

Tailscale gives you private access, but the published port is still bound to
all interfaces by default. Firewall the public side:

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow in on tailscale0      # allow tailnet traffic
sudo ufw allow 22                    # keep SSH (or also restrict to tailscale0)
sudo ufw enable
```

Alternatively, bind the port only to the tailscale IP by editing
`docker-compose.yml`:

```yaml
ports:
  - "100.x.y.z:8080:8080"
```

### Optional: shared-secret token

For an extra layer (e.g. if others share the tailnet), set a token:

```bash
# in .env
ECHO_WEB_TOKEN=some-long-random-string
docker compose up -d
```

Then enter it once in the UI under the ⚙️ panel — it is stored in the browser
and sent with every request.

## 5. Auto-deploy with GitHub Actions

Every push to `main` triggers [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml):

1. Build the image and push `sudami/echo_english:latest` (+ a `:<commit-sha>`
   tag) to Docker Hub.
2. SSH into the VPS and run `git pull && docker compose pull && docker compose up -d`.

So a `git push` deploys in real time — no polling.

### Required GitHub repository secrets

In the repo: **Settings → Secrets and variables → Actions → New repository secret**.

| Secret | Value |
|---|---|
| `DOCKERHUB_USERNAME` | `sudami` |
| `DOCKERHUB_TOKEN` | A Docker Hub **access token** (Account Settings → Security → New Access Token) |
| `SSH_HOST` | VPS public IP / hostname |
| `SSH_USER` | SSH user that owns the deploy checkout (e.g. `deploy` or `root`) |
| `SSH_KEY` | The **private** key whose public half is in the VPS `authorized_keys` |
| `SSH_PORT` | *(optional)* SSH port, defaults to `22` |
| `DEPLOY_PATH` | Absolute path of the repo checkout on the VPS, e.g. `/opt/echoEnglish` |

### One-time VPS setup for the deploy

```bash
# 1. Clone the repo where DEPLOY_PATH points, put creds/.env in place,
#    and do the first start (section 2 + 3).

# 2. Create a dedicated deploy keypair (on your laptop or the VPS):
ssh-keygen -t ed25519 -f deploy_key -N "" -C "github-actions-deploy"

# 3. Authorize the public key on the VPS:
cat deploy_key.pub >> ~/.ssh/authorized_keys     # for the SSH_USER account

# 4. Paste the PRIVATE key (contents of deploy_key) into the SSH_KEY secret.
```

> The deploy account needs permission to run `docker` (in the `docker` group)
> and to `git pull` in `DEPLOY_PATH`.

> **Note:** if you keep the Docker Hub repo private, also run `docker login` once
> on the VPS so `docker compose pull` during deploy is authenticated.

The first workflow run will fail until all secrets are set — that's expected.
You can re-run it from the **Actions** tab once they're in place, or just push
again.

## 6. Usage notes

- **Persistence:** generated jobs live in `./outputs` on the host (a mounted
  volume), so history survives container restarts. Delete from the UI or
  `rm -rf outputs/<job-id>`.
- **Language:** the UI "目标语言 (target language)" picks `ja`/`en`; the native
  side stays Chinese from `config.yaml` — matching the 日→中→日 workflow.
- **Split output:** the "拆成 _tnt 和 _tst" toggle produces two files per job.
- **Updating:** just `git push` to `main` — CI rebuilds and redeploys. To roll
  out manually on the VPS: `git pull && docker compose pull && docker compose up -d`.

## 7. Troubleshooting

- **`pipeline produced no audio output` / Google errors:** check
  `docker compose logs -f` — usually the credentials file isn't mounted or the
  service account lacks the TTS role.
- **edge-tts 503s on long batches:** retry, or split the text; the engine
  retries automatically a few times.
- **Audio won't seek on iOS:** make sure you reach it over the mounted port
  directly; `FileResponse` already supports range requests.
