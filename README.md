# wp-migrate-kit

Bash CLI для переноса WordPress на Linux VPS с Docker Engine. Восстанавливает таблицы MySQL/MariaDB и файлы `wp-content` из UpdraftPlus или обычного бэкапа. Источник может быть локальным либо в Cloudflare R2/S3. На исходном хостинге SSH не нужен.

## Интерактивный запуск

В root-сеансе Bash на VPS запустите одной строкой. Git и предварительное скачивание репозитория не требуются.

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Al3xanderMakunin/wp-migrate-kit/refs/heads/main/dist/wp-migrate)
```

Мастер предложит миграцию или откат. Он покажет контейнеры и точки монтирования, запросит параметры БД, источник и формат бэкапа. Пароль БД и ключи S3 вводятся без отображения. Затем мастер проверит цель и выполнит dry-run. Для записи данных он попросит подтверждение, остановит WordPress при необходимости и предложит запустить его после успешной операции. Откат доступен из того же меню. При восстановлении из S3 после dry-run архивы скачиваются повторно, и мастер снова запрашивает ключи.

Перед запуском подготовьте целевой сайт и установите зависимости по разделу ниже. Для автоматизации и ручного управления этапами можно по-прежнему передавать команды и параметры напрямую.

## 1. Подготовьте новый сайт

1. Разверните WordPress и отдельный контейнер MySQL/MariaDB в Dokploy как **Docker Compose**. Запустите их хотя бы один раз, чтобы установщик образа создал каталог `wp-content`. Держите новый сайт за закрытым маршрутом до проверки миграции. Сохраните отдельную копию томов и конфигурации VPS.
2. Сверьте версии PHP, WordPress и сервера БД с исходным сайтом. На целевом сайте задайте исходный префикс таблиц в `wp-config.php` или через `WORDPRESS_TABLE_PREFIX`, если это поддерживает ваш образ. Проверьте, что WordPress подключён к той же схеме, которую вы передадите в `--database`.
3. Подготовьте **отдельную схему только для этого WordPress**. Пользователю миграции нужны права на чтение, дамп и пересоздание этой схемы. Используйте отдельного локального пользователя БД с правами только на неё, например `GRANT ALL PRIVILEGES ON wordpress.* TO 'wp_migrator'@'localhost';`. Для существующих представлений, триггеров, событий и процедур заранее проверьте права и переносимость `DEFINER` на тестовой копии. Проверьте, что пользователи приложения и миграции сохраняют свои права после пересоздания схемы.
4. В мастере можно ввести логин и пароль вручную. Он создаст временный файл настроек внутри контейнера и удалит его при завершении. Для запуска с параметрами используйте заранее подготовленный приватный файл клиента, например `/run/secrets/wp-migrate.cnf`, с правами `0600`. Клиент запустится внутри контейнера БД и подключится через локальный Unix-сокет.

   ```ini
   [client]
   user=wp_migrator
   password=REPLACE_ON_SERVER
   ```

Установите зависимости на VPS. Пример для Debian/Ubuntu с установленным Docker Engine.

```bash
sudo apt-get update
sudo apt-get install -y curl python3 rsync util-linux coreutils
curl -fsSL https://raw.githubusercontent.com/Al3xanderMakunin/wp-migrate-kit/main/dist/wp-migrate -o wp-migrate && bash wp-migrate --help
```

CLI запускается от root на хосте локального Docker Engine. Он берёт путь к данным из `docker inspect`. Поддерживаются обычные локальные named volumes и bind mounts. Для удалённого Docker, rootless Docker, user namespace remapping, сетевых томов, вложенных монтирований внутри `wp-content` и Swarm нужен другой способ восстановления.

## 2. Выберите источник

Выполняйте все команды ниже в одном сеансе Bash из каталога репозитория.

**UpdraftPlus.** Скачайте все части одного полного набора. Для `backup_2026-01-01-0000_site_123456789abc-uploads2.zip` значение `--set` равно `backup_2026-01-01-0000_site_123456789abc`. Поддерживаются `plugins`, `themes`, `uploads`, `others` и `db.gz`. Сохраните оригинальные имена и ZIP-файлы. CLI обнаруживает пропуски внутри последовательности частей. Сверьте количество файлов с UpdraftPlus: отсутствие последней части или целой категории по именам оставшихся файлов определить нельзя. Зашифрованная БД, инкрементальные наборы и дополнительные категории вроде `wpcore` находятся за пределами этого сценария. [Ручное восстановление UpdraftPlus](https://teamupdraft.com/docs/updraftplus/restoration/how-to/restore-manually-without-wp-admin/).

```bash
source_args=(
  --updraft /srv/backups/updraft
  --set backup_2026-01-01-0000_site_123456789abc
)
```

**Обычный бэкап.** Нужен ZIP или TAR (`.tar`, `.tar.gz`, `.tgz`, `.tar.bz2`, `.tar.xz`) с каталогом `wp-content/` на верхнем уровне, плюс `.sql` или `.sql.gz` одной схемы. Экспортируйте SQL с командами `CREATE TABLE`, без `--databases` и без `USE`. CLI берёт из файлового архива только содержимое `wp-content/`. Симлинки, специальные файлы и дублирующиеся файлы внутри восстанавливаемой части отклоняются. Используйте доверенные копии: PHP из архива будет исполняться сайтом, а SQL импортируется с правами пользователя миграции.

```bash
source_args=(
  --files /srv/backups/site.tar.gz
  --sql /srv/backups/wordpress.sql.gz
)
```

**Cloudflare R2 / S3.** Установите [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html). Для ключей нужен доступ на чтение объектов и, для UpdraftPlus, листинг нужного префикса. В префиксе набора держите архивы на одном уровне, без вложенных каталогов.

С `--s3-prompt` команды `dry-run` и `apply` запросят Access Key ID, Secret Access Key и необязательный Session Token. Ввод скрыт. CLI передаёт ключи AWS CLI через окружение на время скачивания, затем продолжает выбранную операцию. Повторный запуск запрашивает ключи заново. Для стандартного адреса R2 регион `auto` выбирается автоматически. Для AWS S3 опустите `--endpoint` и при необходимости добавьте, например, `--region eu-west-1`.

```bash
source_args=(
  --s3 s3://YOUR_BUCKET/site/migration/
  --endpoint https://YOUR_ACCOUNT_ID.r2.cloudflarestorage.com
  --s3-prompt
  --set backup_2026-01-01-0000_site_123456789abc
)
# Для обычного бэкапа вместо --set укажите --files site.tar.gz --sql wordpress.sql.gz.
```

Для автоматизации настройте `sudo aws configure --profile wp-r2` и замените `--s3-prompt` на `--profile wp-r2`.

Пример запуска `dry-run` одной строкой без клонирования. Подставьте имена контейнеров, путь к конфигурации клиента БД, адрес R2 и имя своего набора.

```bash
(script=$(mktemp) && trap 'rm -f -- "$script"' EXIT && curl -fsSL https://raw.githubusercontent.com/Al3xanderMakunin/wp-migrate-kit/main/dist/wp-migrate -o "$script" && sudo bash "$script" dry-run --wp wordpress --db mariadb --database wordpress --db-cnf /run/secrets/wp-migrate.cnf --prefix wp_ --s3 s3://YOUR_BUCKET/site/migration/ --endpoint https://YOUR_ACCOUNT_ID.r2.cloudflarestorage.com --s3-prompt --set backup_2026-01-01-0000_site_123456789abc)
```

[Подключение AWS CLI к R2](https://developers.cloudflare.com/r2/examples/aws/aws-cli/).

## 3. Проверьте цель и архивы

Узнайте имена контейнеров и точки монтирования.

```bash
sudo docker ps -a --format '{{.Names}}\t{{.Image}}'
sudo docker inspect wordpress --format '{{json .Mounts}}'
```

```bash
target_args=(
  --wp wordpress
  --db mariadb
  --database wordpress
  --db-cnf /run/secrets/wp-migrate.cnf
  --prefix wp_
  --state /var/lib/wp-migrate-kit
)
# Если том подключён прямо к wp-content:
# target_args+=(--mount /var/www/html/wp-content)
# Для нестандартного пути укажите и --mount, и --content.
# Укажите фактические UID/GID на хосте при необходимости:
# target_args+=(--uid 33 --gid 33)

sudo bash wp-migrate preflight "${target_args[@]}" "${source_args[@]}"
sudo bash wp-migrate dry-run "${target_args[@]}" "${source_args[@]}"
```

`preflight` проверяет точку монтирования, доступность клиента и подключение к БД. `dry-run` скачивает и распаковывает резервную копию во временный каталог, проверяет пути, формат, размер и ожидаемые таблицы. Он не останавливает контейнеры, не создаёт постоянный snapshot и не импортирует SQL. Успех dry-run не подтверждает совместимость каждой команды SQL с сервером.

Лимит `--max-gib` ограничивает суммарный объём распаковываемых файлов и SQL, по умолчанию 50 ГиБ. Отдельно выделите место под скачанные архивы, текущие данные сайта, snapshot, файлы импорта и рост БД. Проверьте файловые системы временного каталога, `--state`, томов сайта и БД. Для dry-run можно выделить временный диск через `sudo env TMPDIR=/path/to/private/tmp bash wp-migrate dry-run …`.

## 4. Выполните миграцию

1. Отключите авторазвёртывание и процессы, которые могут записывать данные, включая cron, воркеры и обработчики очередей. Остановите WordPress, оставив БД запущенной. Отложите повторное развёртывание до конца миграции или отката. WordPress запускается вручную после завершения операции.
2. Запустите `apply` в том же сеансе Bash. CLI сначала сохраняет текущее содержимое `wp-content` и SQL-дамп схемы, затем пересоздаёт **всю** целевую схему, импортирует SQL и накладывает файлы. Файлы, отсутствующие в бэкапе, обычно сохраняются. Поэтому для первого переноса используйте чистую цель. Ядро WordPress, конфигурацию, окружение Docker и настройки Traefik обслуживайте на целевой стороне.
3. Пока внешний маршрут закрыт, запустите WordPress и проверьте главную страницу, вход, загрузку медиа, активные плагины и HTTPS. Если домен изменился, используйте WP-CLI в окружении WordPress для замены URL с учётом сериализованных данных. Сначала выполните `wp search-replace 'https://old.example' 'https://new.example' --all-tables-with-prefix --skip-columns=guid --dry-run`. После проверки повторите без `--dry-run` и проверьте `home` и `siteurl`. Проверьте, учитывает ли ваш образ заголовок `X-Forwarded-Proto` от Traefik.
4. После проверки откройте маршрут и включите фоновые задачи. Повтор того же `apply` после успешной миграции идемпотентен для последнего бэкапа и тех же параметров. Он сверяет отпечаток содержимого; последующие изменения живого сайта не перезаписываются при совпадении отпечатка.

```bash
sudo docker stop wordpress
sudo bash wp-migrate apply "${target_args[@]}" "${source_args[@]}" --yes
# Запускайте WordPress только после успешного завершения apply.
sudo docker start wordpress
```

## 5. Откат

При ошибке импорта или копирования после записи snapshot CLI пытается восстановить прежние файлы и БД. Если процесс принудительно убили, хост перезагрузился или автоматический откат не удался, остановите WordPress и запустите ручной откат. `rollback` использует последний snapshot для данного пути к файлам, ID контейнера БД и схемы. Для повторного вызова уже завершённого отката результат будет тем же. Файлы и записи, созданные **после** snapshot, при откате удаляются.

```bash
sudo docker stop wordpress
sudo bash wp-migrate rollback "${target_args[@]}" --yes
sudo docker start wordpress
```

Сохраняйте каталог `--state` на локальном диске, под путём, доступным для записи только root. Используйте один каталог состояния для операций на хосте. В нём могут содержаться персональные данные, хэши паролей и закрытые файлы. Храните его вне репозитория и бакетов с публичным доступом. CLI сохраняет старые snapshot, а автоматический выбор работает только для последнего. Сохраняйте отдельную резервную копию VPS: локальный snapshot зависит от сохранности хоста и контейнера БД. При замене контейнера БД потребуется ручное восстановление с проверенной копии. Для нескольких сайтов переносите каждый сайт отдельно. Для WordPress Multisite остановитесь на этапе dry-run и подготовьте отдельную процедуру смены доменов, путей и сетевых параметров.

## Тесты

```bash
bash -n wp-migrate && bash -n tests/integration.sh
python3 tools/build-standalone.py --check
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
sudo bash tests/integration.sh
sudo env TEST_MOUNT=bind bash tests/integration.sh
```

Интеграционные тесты запускают самодостаточную сборку в Docker с временными контейнерами MariaDB 11.4 и Alpine. GitHub Actions проверяет named volume и bind mount. Загрузка из S3 проверяется с подставным AWS CLI, интерактивным вводом через псевдотерминал и раздельными архивами UpdraftPlus. Проверка с реальным бакетом проводится через dry-run перед переключением трафика.
