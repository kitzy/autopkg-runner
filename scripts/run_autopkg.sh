#!/usr/bin/env bash
set -euo pipefail
AUTOPKG_CMD="${AUTOPKG_CMD:-autopkg}"

MAP_FILE="config/recipe-map.yml"
OUT_DIR="out"
mkdir -p "$OUT_DIR"

read_map() {
  local recipe="$1"
  python3 - "$recipe" "$MAP_FILE" <<'PY'
import sys
from ruamel.yaml import YAML
recipe, map_path = sys.argv[1:3]
yaml = YAML(typ="safe")
with open(map_path) as f:
    data = yaml.load(f) or {}
defaults = data.get('defaults', {})
overrides = data.get('recipes', {}).get(recipe, {})
team = overrides.get('team_id', defaults.get('team_id', 0))
self_service = overrides.get('self_service', defaults.get('self_service', True))
print(f"{team} {str(self_service).lower()}")
PY
}

slugify() {
  python3 - "$1" <<'PY'
import re, sys
name = sys.argv[1]
print(re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-'))
PY
}

while IFS= read -r recipe; do
  [[ -n "$recipe" ]] || continue
  echo "Running $recipe"
  $AUTOPKG_CMD run "overrides/overrides/${recipe}.recipe" --report-plist=report.plist -v
  PKG=$(/usr/libexec/PlistBuddy -c 'Print :results:packages:0:pathname' report.plist)
  read TEAM SELF_SERVICE < <(read_map "$recipe")
  RESPONSE=$(curl -sS -X POST "$FLEET_URL/api/v1/fleet/software/package" \
    -H "Authorization: Bearer $FLEET_API_TOKEN" -H "kbn-xsrf: true" \
    -F team_id="$TEAM" -F self_service="$SELF_SERVICE" \
    -F "software=@$PKG;type=application/octet-stream")
  read NAME VERSION HASH TITLE_ID <<< "$(python3 - "$RESPONSE" <<'PY'
import sys, json
resp=json.loads(sys.argv[1])
print(resp.get('name') or resp.get('software',{}).get('name',''))
print(resp.get('version',''))
print(resp.get('hash_sha256',''))
print(resp.get('title_id') or resp.get('software',{}).get('title_id',''))
PY
)"
  SLUG=$(slugify "$NAME")
  python3 - "$OUT_DIR/$SLUG.json" "$NAME" "$VERSION" "$HASH" "$TITLE_ID" "$SELF_SERVICE" <<'PY'
import sys, json
path, name, version, hash_, title_id, self_service = sys.argv[1:]
json.dump({"name": name, "version": version, "hash": hash_, "title_id": title_id, "self_service": self_service == 'true'}, open(path, 'w'))
PY
done < overrides/recipe-lists/darwin-prod.txt
