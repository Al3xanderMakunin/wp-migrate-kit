#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

[[ $EUID == 0 ]] || { echo 'Run this isolated Docker test as root'; exit 1; }
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
scratch=$(mktemp -d /var/lib/wp-migrate-test.XXXXXXXX)
name="wpmk-${scratch##*.}"
db="$name-db"
wp="$name-wp"
volume="$name-files"
db_volume="$name-dbdata"
cleanup() {
  docker rm -fv "$wp" "$db" >/dev/null 2>&1 || true
  docker volume rm "$volume" "$db_volume" >/dev/null 2>&1 || true
  rm -rf -- "$scratch"
}
trap cleanup EXIT

docker volume create "$volume" >/dev/null
docker volume create "$db_volume" >/dev/null
target=$(docker volume inspect "$volume" --format '{{.Mountpoint}}')
if [[ ${TEST_MOUNT:-volume} == bind ]]; then
  mount="type=bind,src=$target,dst=/var/www/html/wp-content"
else
  mount="type=volume,src=$volume,dst=/var/www/html/wp-content"
fi
# These credentials are disposable test fixtures. No database port is published.
docker run -d --name "$db" --network none --mount "type=volume,src=$db_volume,dst=/var/lib/mysql" \
  -e MARIADB_ROOT_PASSWORD=fixture-only -e MARIADB_DATABASE=wordpress mariadb:11.4 >/dev/null
docker create --name "$wp" --network none --mount "$mount" alpine:3.22 true >/dev/null
docker exec "$db" sh -ec '
  umask 077
  printf "[client]\nuser=root\npassword=fixture-only\n" > /tmp/wp-fixture.cnf'
db_query() { docker exec -i "$db" mariadb --defaults-file=/tmp/wp-fixture.cnf -N "$@"; }
ready=false
for _ in {1..60}; do
  if db_query -e 'SELECT 1' >/dev/null 2>&1 &&
    docker exec "$db" sh -c 'test "$(cat /proc/1/comm)" = mariadbd'; then
    ready=true
    break
  fi
  sleep 1
done
$ready || { docker logs "$db"; exit 1; }

db_query wordpress -e 'CREATE TABLE original (id INT); INSERT INTO original VALUES (7)'
printf 'original\n' > "$target/original.txt"
mkdir -p "$scratch/source/wp-content/uploads"
printf 'new\n' > "$scratch/source/wp-content/uploads/test.txt"
tar -C "$scratch/source" -czf "$scratch/files.tar.gz" wp-content
cat > "$scratch/wordpress.sql" <<'SQL'
CREATE TABLE wp_options (id INT);
CREATE TABLE wp_posts (id INT);
CREATE TABLE wp_users (id INT);
INSERT INTO wp_posts VALUES (42);
SQL
cp "$scratch/wordpress.sql" "$scratch/broken.sql"
printf 'THIS IS INVALID SQL;\n' >> "$scratch/broken.sql"
base=(--wp "$wp" --db "$db" --database wordpress --db-cnf /tmp/wp-fixture.cnf
  --prefix wp_ --state "$scratch/state" --mount /var/www/html/wp-content)
source=(--files "$scratch/files.tar.gz" --sql "$scratch/wordpress.sql")
run() {
  local action=$1
  shift
  bash "$root/wp-migrate" "$action" "${base[@]}" "$@"
}

run preflight "${source[@]}"
run dry-run "${source[@]}"
[[ ! -e $scratch/state && ! -e $target/uploads ]]
[[ $(db_query wordpress -e 'SELECT id FROM original') == 7 ]]
if run apply "${source[@]}"; then echo 'Apply succeeded without --yes'; exit 1; fi
if run apply --files "$scratch/files.tar.gz" --sql "$scratch/broken.sql" --yes; then
  echo 'Broken SQL unexpectedly succeeded'
  exit 1
fi
[[ ! -e $target/uploads && $(db_query wordpress -e 'SELECT id FROM original') == 7 ]]
run apply "${source[@]}" --yes
[[ $(cat "$target/uploads/test.txt") == new ]]
[[ $(db_query wordpress -e 'SELECT id FROM wp_posts') == 42 ]]
db_query wordpress -e 'INSERT INTO wp_posts VALUES (43)'
run apply "${source[@]}" --yes
[[ $(db_query wordpress -e 'SELECT COUNT(*) FROM wp_posts') == 2 ]]
run rollback --yes
run rollback --yes
[[ ! -e $target/uploads && $(cat "$target/original.txt") == original ]]
[[ $(db_query wordpress -e 'SELECT id FROM original') == 7 ]]
echo "Integration passed ($mount)"
