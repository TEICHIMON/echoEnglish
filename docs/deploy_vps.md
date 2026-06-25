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

## 4. Put it behind Caddy (HTTPS on your domain)

The container binds to `127.0.0.1:8080` only, so it is not reachable from the
public internet directly — only the host's Caddy can reach it. Add a site block
to your Caddyfile (usually `/etc/caddy/Caddyfile`):

```caddyfile
echo.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

Reload Caddy and open the site:

```bash
sudo systemctl reload caddy
# then browse to https://echo.example.com  (Caddy gets the TLS cert automatically)
```

Make sure the firewall allows Caddy's ports (and SSH for CI deploys):

```bash
sudo ufw allow 80
sudo ufw allow 443
sudo ufw allow 22
```

### Gate the public site (recommended)

A public domain is reachable by anyone — add a login. Either option works; the
audio `<audio>` element keeps playing because the browser resends credentials.

**Option A — Caddy basic auth** (handled at the proxy, before the app):

```bash
caddy hash-password         # type a password, copy the bcrypt hash it prints
```

```caddyfile
echo.example.com {
    reverse_proxy 127.0.0.1:8080
    basic_auth {              # `basicauth` on Caddy < 2.8
        yourname JDJhJDE0...<bcrypt-hash>
    }
}
```

**Option B — app token** instead of Caddy auth: set `ECHO_WEB_TOKEN` in `.env`,
`docker compose up -d`, then enter it once in the UI under the ⚙️ panel.

> **Alternative to Caddy:** if you'd rather keep it private with no domain, run
> Tailscale on the VPS and phone, set `HOST_BIND` to the tailscale IP, and
> browse `http://<tailscale-ip>:8080`.

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

## 6. Sync generated audio to Google Drive (rclone)

Optionally upload every finished job's audio (+ `.lrc`) to Google Drive. A
headless VPS can't run Google Drive for Desktop, so it uses **rclone** with a
Google Drive remote. One-time setup:

1. **On a machine with a browser** (e.g. your laptop), create/authorize a Drive
   remote named `gdrive` (or refresh it if its token went stale):

   ```bash
   rclone config              # n) new remote -> name: gdrive -> storage: drive -> browser auth
   rclone config reconnect gdrive:   # use this if you already have it but the token expired
   rclone lsd gdrive:         # sanity check: should list your My Drive folders
   ```

2. **Copy the rclone config to the VPS.** It holds the OAuth token, so it is
   gitignored and never baked into the image:

   ```bash
   scp ~/.config/rclone/rclone.conf <user>@<vps>:<DEPLOY_PATH>/rclone.conf
   ```

   docker-compose mounts it at `/root/.config/rclone/rclone.conf` (read-write so
   rclone can keep its short-lived access token fresh).

3. **Enable sync in `.env`:**

   ```bash
   ECHO_SYNC_ENABLED=true
   ECHO_SYNC_DEST=gdrive:echoEnglish    # a folder in My Drive (created automatically)
   # ECHO_SYNC_METHOD=rclone            # already the docker-compose default
   # ECHO_SYNC_LAYOUT=run_folder        # one timestamped subfolder per job
   ```

4. **Apply:** `docker compose up -d` (env changes take effect on `up`; add
   `--build` if you also pulled new code).

Each job then uploads to
`gdrive:echoEnglish/<YYYY-MM-DD_HH-MM-SS_Language>/…`. Sync runs *after* the audio
is saved and **never fails a job** — issues are logged as warnings only. Watch
`docker compose logs -f` for `✓ Synced N file(s) to: …` or `Drive sync failed: …`.

> The "Computers › My MacBook Pro › audio" folder you may see in Drive is a
> *backup of your Mac's local folder*, not a normal Drive folder — uploading
> there from a server is unreliable and would sync back down onto your Mac. Use a
> plain My Drive path like `gdrive:echoEnglish` instead.

## 7. Usage notes

- **Persistence:** generated jobs live in `./outputs` on the host (a mounted
  volume), so history survives container restarts. By default they are kept
  forever — delete from the UI or `rm -rf outputs/<job-id>`. To auto-clean, set
  `ECHO_RETENTION_DAYS` (drop jobs older than N days) and/or `ECHO_MAX_JOBS`
  (keep only the newest N) in `.env`.
- **Language:** the UI "目标语言 (target language)" picks `ja`/`en`; the native
  side stays Chinese from `config.yaml` — matching the 日→中→日 workflow.
- **Split output:** the "拆成 _tnt 和 _tst" toggle produces two files per job.
- **Updating:** just `git push` to `main` — CI rebuilds and redeploys. To roll
  out manually on the VPS: `git pull && docker compose pull && docker compose up -d`.

## 8. Troubleshooting

- **`pipeline produced no audio output` / Google errors:** check
  `docker compose logs -f` — usually the credentials file isn't mounted or the
  service account lacks the TTS role.
- **Drive sync did nothing:** look for `Drive sync` lines in `docker compose
  logs`. Common causes: `ECHO_SYNC_ENABLED` isn't `true`; `rclone.conf` is
  missing/empty on the host (the bind mount then creates an empty file/dir); or
  the token expired (`invalid_grant`) — refresh with `rclone config reconnect
  gdrive:` on a browser machine and re-copy `rclone.conf`, then `docker compose
  up -d`.
- **edge-tts 503s on long batches:** retry, or split the text; the engine
  retries automatically a few times.
- **Audio won't seek on iOS:** make sure you reach it over the mounted port
  directly; `FileResponse` already supports range requests.
