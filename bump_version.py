#!/usr/bin/env python3
import re
import subprocess
import sys
import toml


def bump_version(current_version: str, bump_type: str) -> str:
    """
    Bump a semantic version string.

    Assumes a version format like: X.Y.Z or X.Y.Z<tag><number>
    where tag can be letters (e.g. 'b' for beta) and number is an integer.
    """
    pattern = r"^(\d+)\.(\d+)\.(\d+)([a-zA-Z]+\d+)?$"
    m = re.match(pattern, current_version)
    if not m:
        raise ValueError(
            f"Version '{current_version}' does not match the expected format (e.g. 1.2.3 or 1.2.3b0)."
        )

    major, minor, patch, pre = m.groups()
    major = int(major)
    minor = int(minor)
    patch = int(patch)

    if bump_type == "major":
        major += 1
        minor = 0
        patch = 0
        new_version = f"{major}.{minor}.{patch}"
    elif bump_type == "minor":
        minor += 1
        patch = 0
        new_version = f"{major}.{minor}.{patch}"
    elif bump_type == "patch":
        patch += 1
        new_version = f"{major}.{minor}.{patch}"
    elif bump_type == "prerelease":
        # If already a prerelease version, bump the numeric part.
        if pre:
            tag = re.match(r"([a-zA-Z]+)", pre).group(1)
            num = int(re.search(r"(\d+)$", pre).group(1))
            num += 1
            new_version = f"{major}.{minor}.{patch}{tag}{num}"
        else:
            # If no prerelease tag, add one (defaulting to beta 0)
            new_version = f"{major}.{minor}.{patch}b0"
    elif bump_type == "release":
        # For a release, remove any prerelease tag (i.e. make it a stable version).
        new_version = f"{major}.{minor}.{patch}"
    else:
        raise ValueError(
            "Invalid bump type. Choose one of: major, minor, patch, prerelease, release"
        )

    return new_version


def main():
    if len(sys.argv) != 2:
        print("Usage: bump_version.py [major|minor|patch|prerelease|release]")
        sys.exit(1)

    bump_type = sys.argv[1]

    # Load the current pyproject.toml
    try:
        with open("pyproject.toml", "r") as f:
            config = toml.load(f)
    except Exception as e:
        print(f"Error reading pyproject.toml: {e}")
        sys.exit(1)

    # Get the current version from [tool.poetry]
    try:
        current_version = config["tool"]["poetry"]["version"]
    except KeyError:
        print("Could not find [tool.poetry] version in pyproject.toml")
        sys.exit(1)

    try:
        new_version = bump_version(current_version, bump_type)
    except ValueError as err:
        print(err)
        sys.exit(1)

    # Update the version in the config
    config["tool"]["poetry"]["version"] = new_version

    # Write back the updated pyproject.toml
    try:
        with open("pyproject.toml", "w") as f:
            toml.dump(config, f)
    except Exception as e:
        print(f"Error writing pyproject.toml: {e}")
        sys.exit(1)

    print(f"Bumped version from {current_version} to {new_version}")

    # Set up Git configuration (so that the commit shows a valid author)
    subprocess.run(["git", "config", "user.name", "github-actions"], check=True)
    subprocess.run(
        ["git", "config", "user.email", "github-actions@github.com"], check=True
    )

    # Add the file and commit
    subprocess.run(["git", "add", "pyproject.toml"], check=True)
    commit_message = f"Bump version: {current_version} -> {new_version}"
    subprocess.run(["git", "commit", "-m", commit_message], check=True)

    # Push the commit (this requires that your GitHub token is set up so that pushing is allowed)
    subprocess.run(["git", "push"], check=True)


if __name__ == "__main__":
    main()
