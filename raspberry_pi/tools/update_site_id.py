#!/usr/bin/env python3
"""
update_site_id.py

Surgically updates only the top-level `site_id:` line in a
site_config.yaml file, leaving every other line (comments, formatting,
other settings) byte-for-byte untouched. A full YAML load+dump round-trip
would work for the *data*, but would reformat the file and lose comments
-- this instead does a targeted single-line text replacement, then
validates the result by re-parsing it as YAML.

Used by finalize_clone.sh so that cloning a verified master's
site_config.yaml and then re-running this to assign a new site_id doesn't
touch anything else in the file (server host, MQTT tuning, etc.).

Usage:
  python update_site_id.py <path-to-site_config.yaml> <new-site-id>

Exits 0 and prints the new site_id on success; exits 1 with an error
message on any failure (file not found, no site_id line found, or the
edited file fails to parse as valid YAML afterward).
"""

import re
import sys

import yaml

SITE_ID_LINE_RE = re.compile(r'^(\s*site_id\s*:\s*).*$')


def update_site_id(path: str, new_site_id: str) -> str:
    """Replaces the value on the first top-level `site_id:` line in
    `path` with `new_site_id`, preserving every other line exactly.
    Returns the new line that was written. Raises ValueError if no
    site_id line is found, or if the result doesn't parse as valid YAML
    with the expected site_id afterward."""
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()

    new_line = None
    for i, line in enumerate(lines):
        match = SITE_ID_LINE_RE.match(line)
        if match:
            new_line = f'{match.group(1)}"{new_site_id}"\n'
            lines[i] = new_line
            break

    if new_line is None:
        raise ValueError(f"No 'site_id:' line found in {path}")

    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)

    with open(path, encoding="utf-8") as f:
        reparsed = yaml.safe_load(f)
    if not reparsed or reparsed.get("site_id") != new_site_id:
        raise ValueError(
            f"Post-write validation failed: site_id is "
            f"{(reparsed or {}).get('site_id')!r}, expected {new_site_id!r}"
        )

    return new_line


def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: update_site_id.py <path-to-site_config.yaml> <new-site-id>", file=sys.stderr)
        sys.exit(1)

    path, new_site_id = sys.argv[1], sys.argv[2]
    try:
        update_site_id(path, new_site_id)
    except (OSError, ValueError, yaml.YAMLError) as e:
        print(f"[FATAL] {e}", file=sys.stderr)
        sys.exit(1)

    print(new_site_id)


if __name__ == "__main__":
    main()
