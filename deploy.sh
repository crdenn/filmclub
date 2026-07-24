#!/bin/bash
# One-shot build + run for Unraid. Run this from inside the filmclub folder.
set -e
cd "$(dirname "$0")"
echo ">> Creating appdata dir..."
mkdir -p /mnt/user/appdata/filmclub/data
echo ">> Building image (first build pulls base image, ~1-2 min)..."
docker build -t filmclub:latest .
echo ">> (Re)starting container..."
docker rm -f filmclub 2>/dev/null || true
docker run -d --name filmclub \
  --restart unless-stopped \
  -p 8000:8000 \
  --env-file "$(pwd)/.env" \
  -v /mnt/user/appdata/filmclub/data:/data \
  filmclub:latest
echo ""
echo ">> Done. FilmClub is live on this box at port 8000."
echo ">> Point your Cloudflare tunnel hostname at this box on port 8000 (http://<server-ip>:8000)."
