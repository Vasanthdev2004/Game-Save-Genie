# Self-host your own save server

Keep everything in-house: run an S3-compatible object store on your own
hardware and point Game Save Genie at it. No Google, no Railway, no
third-party storage — your saves never leave your network.

Any S3-compatible server works (MinIO, Garage, SeaweedFS, TrueNAS's built-in
MinIO, an existing homelab MinIO…). This folder ships a ready-to-run MinIO
compose file.

## Quick start

On the server:

```bash
cd docker
cp .env.example .env    # edit both credentials!
docker compose up -d
```

On each gaming machine:

```bash
gsg setup-s3 homelab
# endpoint:   http://<server-ip>:9000
# access key: your MINIO_ROOT_USER (or a per-user key, below)
# secret key: your MINIO_ROOT_PASSWORD
# bucket:     game-saves
```

Setup verifies bucket access before declaring success. From then on
`gsg auto` backs up to your server, and every machine you configure the
same way shares the save history (`gsg pull --all` catches a machine up).

## Per-friend accounts

Give each person their own credentials and bucket so nobody can touch
anyone else's saves. Using the MinIO console (`http://<server-ip>:9001`)
or `mc`:

```bash
docker exec gsg-minio mc alias set local http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"
docker exec gsg-minio mc mb local/saves-alice
docker exec gsg-minio mc admin user add local alice  ALICE_SECRET_KEY
docker exec gsg-minio mc admin policy attach local readwrite --user alice
```

Alice then runs `gsg setup-s3` with her key/secret and bucket `saves-alice`.
(For strict isolation, attach a per-bucket policy instead of `readwrite`.)

## Behind a domain (gsg.mydomain.com)

Put your usual reverse proxy (Caddy, Traefik, nginx) in front of port 9000
with TLS, then use `https://gsg.mydomain.com` as the endpoint in
`gsg setup-s3`. Nothing else changes — gsg speaks plain S3 over HTTPS.

## Notes

- gsg's delta uploads (content-addressed storage) work unchanged against a
  self-hosted server — only changed save files are transferred.
- Back up the `./data` volume like anything else you care about; it holds
  every version of every save.
- Raise `storage_limit_gb` in `gsg config` (or set `0` to disable the
  quota warning) — homelab disks aren't a 5 GB cloud tier.
