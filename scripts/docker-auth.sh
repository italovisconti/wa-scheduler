#!/usr/bin/env bash
set -euo pipefail

docker compose run --rm web wacli auth --store /wacli-store
