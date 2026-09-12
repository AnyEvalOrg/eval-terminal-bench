"""Run Harbor's export CLI with a guard on the metadata it actually consumes.

Executed as a script in Harbor's interpreter, including when Harbor is on PATH
in a different environment. Keep imports of the optional dependency lazy.
"""
from __future__ import annotations

import runpy
import sys


def main() -> None:
    from harbor.registry.client.package import PackageDatasetClient

    content_reference, version_id, command, *arguments = sys.argv[1:]
    original_argv = sys.argv
    get_metadata = PackageDatasetClient._get_dataset_metadata

    async def checked_metadata(self, name):
        metadata = await get_metadata(self, name)
        if (metadata.dataset_version_content_hash != content_reference.removeprefix("sha256:")
                or metadata.dataset_version_id != version_id
                or metadata.version != content_reference):
            raise ValueError("Registry version does not match pinned version")
        return metadata

    PackageDatasetClient._get_dataset_metadata = checked_metadata
    try:
        sys.argv = [command, *arguments]
        runpy.run_path(command, run_name="__main__")
    finally:
        sys.argv = original_argv
        PackageDatasetClient._get_dataset_metadata = get_metadata


if __name__ == "__main__":
    main()
