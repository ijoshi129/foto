#!/bin/bash
# update-foto.sh — pull the latest foto image and recreate the container,
# reusing settings (image, ports, volumes, env vars) from its dockerMan
# user template. Run on the Unraid box: `./update-foto.sh`.
set -euo pipefail

NAME=foto
TPL_DIR=/boot/config/plugins/dockerMan/templates-user

# Unraid saves user templates as either <name>.xml or my-<name>.xml depending
# on how the container was first created — try both.
for f in "$TPL_DIR/$NAME.xml" "$TPL_DIR/my-$NAME.xml"; do
  if [ -f "$f" ]; then T="$f"; break; fi
done
if [ -z "${T:-}" ]; then
  echo "No template found in $TPL_DIR for '$NAME'" >&2
  exit 1
fi

# Parse the template via python3 (always present on Unraid). Each line of
# output is: TYPE\tTARGET\tDEFAULT\tMODE\tVALUE — Network and Repository are
# emitted with sentinel TYPEs.
PARSED=$(python3 - "$T" <<'PY'
import sys, xml.etree.ElementTree as ET
root = ET.parse(sys.argv[1]).getroot()

def emit(*cols):
    print("\t".join(cols))

emit("Repository", "", "", "", (root.findtext("Repository") or "").strip())
emit("Network",    "", "", "", (root.findtext("Network")    or "bridge").strip())
emit("Privileged", "", "", "", (root.findtext("Privileged") or "false").strip())

for cfg in root.findall("Config"):
    emit(
        cfg.attrib.get("Type", ""),
        cfg.attrib.get("Target", ""),
        cfg.attrib.get("Default", ""),
        cfg.attrib.get("Mode", ""),
        (cfg.text or "").strip(),
    )
PY
)

IMAGE=""
NETWORK="bridge"
PRIVILEGED="false"
ARGS=()

while IFS=$'\t' read -r TYPE TARGET DEFAULT MODE VALUE; do
  # Fall back to template default if the user left the value blank.
  if [ -z "$VALUE" ]; then VALUE="$DEFAULT"; fi
  case "$TYPE" in
    Repository) IMAGE="$VALUE" ;;
    Network)    NETWORK="${VALUE:-bridge}" ;;
    Privileged) PRIVILEGED="$VALUE" ;;
    Port)
      proto="${MODE:-tcp}"
      [ -n "$VALUE" ] && ARGS+=(-p "${VALUE}:${TARGET}/${proto}")
      ;;
    Path)
      [ -n "$VALUE" ] && ARGS+=(-v "${VALUE}:${TARGET}:${MODE:-rw}")
      ;;
    Variable)
      [ -n "$VALUE" ] && ARGS+=(-e "${TARGET}=${VALUE}")
      ;;
    Device)
      [ -n "$VALUE" ] && ARGS+=(--device "${VALUE}:${TARGET}")
      ;;
    Label)
      [ -n "$VALUE" ] && ARGS+=(-l "${TARGET}=${VALUE}")
      ;;
  esac
done <<< "$PARSED"

if [ -z "$IMAGE" ]; then
  echo "Template $T has no <Repository>" >&2
  exit 1
fi

[ "$PRIVILEGED" = "true" ] && ARGS+=(--privileged)
ARGS+=(--network "$NETWORK")

echo "→ Pulling $IMAGE"
docker pull "$IMAGE"

echo "→ Stopping/removing existing '$NAME' container"
docker stop "$NAME" >/dev/null 2>&1 || true
docker rm   "$NAME" >/dev/null 2>&1 || true

echo "→ Starting '$NAME'"
docker run -d \
  --name "$NAME" \
  --restart unless-stopped \
  "${ARGS[@]}" \
  "$IMAGE"

echo "→ Done."
docker ps --filter "name=^${NAME}$" --format "table {{.Names}}\t{{.Image}}\t{{.Status}}"
