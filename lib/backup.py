"""Validate backups in private staging and resolve local Docker storage."""
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tarfile
import zipfile

CATEGORIES = ("plugins", "themes", "uploads", "others")
CHUNK = 1024 * 1024


def require(condition, message):
    if not condition:
        raise ValueError(message)


def archive_path(name):
    require(not re.search(r"[\x00-\x1f\\:]", name), f"Invalid archive path: {name!r}")
    path = PurePosixPath(name)
    require(not path.is_absolute() and ".." not in path.parts, f"Unsafe archive path: {name!r}")
    return path.parts


class Stage:
    def __init__(self, root, limit):
        self.root = root
        self.remaining = limit
        self.entries = 0

    def count(self):
        self.entries += 1
        require(self.entries <= 1_000_000, "Too many archive entries")

    def copy(self, source, destination, expected=None):
        require(expected is None or 0 <= expected <= self.remaining, "Unpacked size limit exceeded")
        destination.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with destination.open("xb") as output:
            while chunk := source.read(CHUNK):
                self.remaining -= len(chunk)
                require(self.remaining >= 0, "Unpacked size limit exceeded")
                output.write(chunk)
                written += len(chunk)
        require(expected is None or written == expected, "Truncated archive entry")

    def destination(self, name, category, directory):
        parts = archive_path(name)
        if not parts:
            require(directory, "Empty filename")
            return None
        if category is None:
            if parts[0] != "wp-content":
                return None
            parts = parts[1:]
        elif category != "others":
            require(parts[0] == category, f"Expected {category}/ at archive root")
        if not parts:
            require(directory, "Expected a wp-content directory")
            return None
        return self.root / "content" / Path(*parts)

    def zip(self, path, category):
        with zipfile.ZipFile(path) as archive:
            for item in archive.infolist():
                self.count()
                directory = item.is_dir()
                mode = stat.S_IFMT(item.external_attr >> 16)
                require(mode in (0, stat.S_IFDIR if directory else stat.S_IFREG),
                        f"Unsupported ZIP entry: {item.filename!r}")
                destination = self.destination(item.orig_filename, category, directory)
                if destination is None:
                    continue
                if directory:
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    with archive.open(item) as stream:
                        self.copy(stream, destination, item.file_size)

    def tar(self, path):
        with tarfile.open(path, "r|*") as archive:
            for item in archive:
                self.count()
                directory = item.isdir()
                require(directory or item.isfile(), f"Unsupported TAR entry: {item.name!r}")
                destination = self.destination(item.name, None, directory)
                if destination is None:
                    continue
                if directory:
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    with archive.extractfile(item) as stream:
                        self.copy(stream, destination, item.size)


def regular(path):
    require(not path.is_symlink() and path.is_file(), f"Expected regular file: {path}")
    return path


def updraft_set(stage, folder, stem):
    require(re.fullmatch(r"backup_[a-zA-Z0-9_.-]+", stem), "Invalid UpdraftPlus stem")
    folder = Path(folder)
    database = regular(folder / (stem + "-db.gz"))
    found = {category: {} for category in CATEGORIES}
    pattern = re.compile(r"(plugins|themes|uploads|others)([2-9]|[1-9][0-9]+)?\.zip")
    for path in folder.glob(stem + "-*"):
        suffix = path.name[len(stem) + 1:]
        if suffix == "db.gz":
            continue
        match = pattern.fullmatch(suffix)
        require(match, f"Unsupported UpdraftPlus component: {suffix}")
        category, number = match.groups()
        found[category][int(number or 1)] = regular(path)
    require(any(found.values()), "No UpdraftPlus file archives found")
    for category, parts in found.items():
        require(all(number == index for index, number in enumerate(sorted(parts), 1)),
                "Missing UpdraftPlus archive part")
        for number in sorted(parts):
            stage.zip(parts[number], category)
    return database


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def tree_hash(root, metadata=False):
    digest = hashlib.sha256()
    require(root.is_dir() and not root.is_symlink(), "Missing content directory")
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.lstat()
        row = [path.relative_to(root).as_posix()]
        if metadata:
            row.extend((info.st_mode, info.st_uid, info.st_gid))
        if stat.S_ISLNK(info.st_mode):
            row.extend(("link", os.readlink(path)))
        elif stat.S_ISREG(info.st_mode):
            row.extend(("file", file_hash(path)))
        elif stat.S_ISDIR(info.st_mode):
            row.append("dir")
        else:
            raise ValueError(f"Unsupported filesystem entry: {path}")
        digest.update(json.dumps(row, ensure_ascii=True).encode() + b"\n")
    return digest.hexdigest()


def stage_backup(root, gib, prefix, folder, stem, files, sql):
    root = Path(root)
    stage = Stage(root, int(gib) * 1024**3)
    if stem:
        database = updraft_set(stage, folder, stem)
    else:
        archive = regular(Path(files))
        if archive.name.endswith(".zip"):
            stage.zip(archive, None)
        elif archive.name.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")):
            stage.tar(archive)
        else:
            raise ValueError("Expected ZIP or TAR archive")
        database = regular(Path(sql))
        require(database.name.endswith((".sql", ".sql.gz")), "Expected .sql or .sql.gz")
    imported = root / "import.sql"
    opener = gzip.open if database.name.endswith(".gz") else open
    # The dump comes from a trusted backup. These checks catch format/prefix mistakes.
    with opener(database, "rb") as stream:
        stage.copy(stream, imported)
    wanted = {prefix.encode() + table for table in (b"options", b"posts", b"users")}
    table_pattern = re.compile(rb"^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:\x60([a-zA-Z0-9_]+)\x60|([a-zA-Z0-9_]+))(?=\s|\()", re.I)
    with imported.open("rb") as stream:
        line_start = True
        while chunk := stream.readline(CHUNK):
            if line_start:
                require(not re.match(rb"^\s*(USE\s|(?:CREATE|DROP|ALTER)\s+DATABASE\b)", chunk, re.I),
                        "Use a single-schema dump without USE or database DDL")
                match = table_pattern.match(chunk)
                if match:
                    wanted.discard(match[1] or match[2])
            line_start = chunk.endswith(b"\n")
    require(not wanted, "SQL dump lacks expected CREATE TABLE statements for the WordPress prefix")
    content = root / "content"
    require(content.is_dir() and any(content.iterdir()), "No wp-content entries found")
    print(file_hash(imported) + ":" + tree_hash(content))


def docker_json(*arguments):
    return json.loads(subprocess.check_output(["docker", *arguments, "--format", "{{json .}}"]))


def overlaps(a, b):
    return a == b or a in b.parents or b in a.parents


def container_path(value):
    path = PurePosixPath(value)
    require(path.is_absolute() and path.as_posix() == value and ".." not in path.parts
            and len(path.parts) > 1 and path.root == "/", "Use a canonical absolute container path")
    return path


def resolve_target(name, mount_path, content_path, mode):
    mount_path, content_path = map(container_path, (mount_path, content_path))
    require(content_path == mount_path or mount_path in content_path.parents,
            "Content path must be inside the selected mount")
    container = docker_json("inspect", "--type", "container", name)
    require("com.docker.swarm.service.id" not in (container["Config"]["Labels"] or {}),
            "Swarm tasks are unsupported")
    if mode == "stopped":
        require(container["State"]["Status"] in {"created", "exited"}, "WordPress must be stopped")
    mounts = container["Mounts"]
    selected = [item for item in mounts if item["Destination"] == str(mount_path)]
    require(len(selected) == 1, "Selected Docker mount does not exist")
    selected = selected[0]
    require(selected["RW"] and selected["Type"] in ("bind", "volume"), "Expected a writable bind mount or local volume")
    for item in mounts:
        other = PurePosixPath(item["Destination"])
        if other != mount_path and mount_path in other.parents:
            require(not overlaps(other, content_path), "Nested Docker mount intersects wp-content")
    source = Path(selected["Source"])
    if selected["Type"] == "volume":
        volume = docker_json("volume", "inspect", selected["Name"])
        require(volume["Driver"] == "local" and not volume.get("Options")
                and volume["Mountpoint"] == str(source), "Expected a standard local Docker volume")
    target = source / content_path.relative_to(mount_path).as_posix()
    require(target.is_absolute() and target != Path("/") and target.is_dir()
            and target.resolve(strict=True) == target, "Content path must be an existing non-symlink directory")
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        escaped = line.split()[4]
        path = Path(re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), escaped))
        require(target not in path.parents, f"Nested host mount under wp-content: {path}")
    if mode == "stopped":
        running = subprocess.check_output(["docker", "ps", "-q"], text=True).split()
        for identifier in running:
            other = docker_json("inspect", "--type", "container", identifier)
            for item in other["Mounts"]:
                if item.get("Source"):
                    require(not overlaps(Path(item["Source"]).resolve(), target),
                            f"Running container shares target storage: {other['Name']}")
    print(target)


def snapshot_manifest(root):
    return file_hash(regular(root / "db.sql")) + ":" + tree_hash(root / "content", metadata=True)


def validate_state(path):
    for parent in [*reversed(Path(path).parents), Path(path)]:
        if parent.exists():
            info = parent.lstat()
            require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not (info.st_mode & 0o022),
                    f"State path must be below root-owned, non-writable directories: {parent}")


def main():
    command, *args = sys.argv[1:]
    if command == "stage":
        stage_backup(*args)
    elif command == "target":
        resolve_target(*args)
    elif command == "state-path":
        validate_state(*args)
    elif command == "seal":
        root = Path(args[0])
        (root / "manifest").write_text(snapshot_manifest(root) + "\n", encoding="ascii")
    elif command == "verify":
        root = Path(args[0])
        require((root / "manifest").read_text(encoding="ascii").strip() == snapshot_manifest(root),
                "Snapshot integrity check failed")
    else:
        raise ValueError("Unknown helper command")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, EOFError, tarfile.TarError, zipfile.BadZipFile,
            subprocess.CalledProcessError) as error:
        sys.exit(f"wp-migrate: {error}")
