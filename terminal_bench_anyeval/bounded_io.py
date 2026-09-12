"""Bounded disk-backed tar transfers and incremental file hashing."""
import hashlib
import io
from pathlib import PurePosixPath
import tarfile


class TransferLimitError(RuntimeError):
    """Transfer byte/member or remote output limit exceeded (infrastructure)."""


class LimitedWriter:
    def __init__(self, stream, limit):
        self.stream, self.limit = stream, limit

    def write(self, data):
        if self.stream.tell() + len(data) > self.limit:
            raise TransferLimitError("Transfer byte limit exceeded")
        return self.stream.write(data)

    def tell(self):
        return self.stream.tell()

    def flush(self):
        return self.stream.flush()


def as_file(data):
    return io.BytesIO(data) if isinstance(data, bytes) else data


def members(archive, byte_limit, member_limit):
    total = 0
    content_sizes = {}
    for index, member in enumerate(archive, 1):
        size = member.size
        if member.islnk():
            target = str(PurePosixPath(member.linkname))
            if target not in content_sizes:
                raise ValueError("Hardlink must refer to an earlier regular file")
            # Filtered downloads materialize each link as a separate file.
            # Charge its resolved size before yielding it to any writer.
            size = content_sizes[target]
        total += size
        if index > member_limit or total > byte_limit:
            raise TransferLimitError("Archive expanded byte/member limit exceeded")
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("Unsafe artifact archive path")
        if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
            raise ValueError("Unsupported artifact type")
        if member.issym() or member.islnk():
            link = PurePosixPath(member.linkname)
            if link.is_absolute() or ".." in link.parts:
                raise ValueError("Unsafe artifact archive link")
        if member.isfile() or member.islnk():
            content_sizes[str(path)] = size
        yield member


def inventory(data, byte_limit, member_limit):
    data = as_file(data)
    data.seek(0, 2)
    if data.tell() > byte_limit:
        raise TransferLimitError("Transfer byte limit exceeded")
    data.seek(0)
    entries = {}
    regular_files = set()
    with tarfile.open(fileobj=data, mode="r:") as archive:
        for member in members(archive, byte_limit, member_limit):
            name = str(PurePosixPath(member.name))
            if name in entries:
                raise ValueError("Duplicate artifact archive path")
            if member.islnk():
                target = str(PurePosixPath(member.linkname))
                if target not in regular_files:
                    raise ValueError("Hardlink must refer to an earlier regular file")
                entries[name] = entries[target]
                regular_files.add(name)
            elif member.isfile():
                digest = hashlib.sha256()
                with archive.extractfile(member) as stream:
                    while chunk := stream.read(65536):
                        digest.update(chunk)
                entries[name] = digest.hexdigest()
                regular_files.add(name)
            elif member.isdir():
                entries[name] = hashlib.sha256(b"directory").hexdigest()
            elif member.issym():
                entries[name] = hashlib.sha256(b"symlink\0" + member.linkname.encode()).hexdigest()
    entries.pop(".", None)
    data.seek(0)
    return entries
