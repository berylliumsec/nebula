import re
import sys

with open("setup.py", "r") as f:
    content = f.read()

# Extract the current version
match = re.search(r'version="([\d.]+)b(\d+)"', content)
if match:
    version = match.group(1)
    beta_num = int(match.group(2))

    new_beta_num = beta_num + 1
    new_version = f"{version}b{new_beta_num}"

    content = content.replace(
        f'version="{version}b{beta_num}"', f'version="{new_version}"'
    )

    print("new version")
    print(f"{new_version}")
    with open("setup.py", "w") as f:
        f.write(content)

    # Output the new version for subsequent GitHub Actions steps
    print(f"::set-output name=new_version::{new_version}")
    new_version_file = "new_version.txt"
    with open(new_version_file, "w") as f:
        f.write(new_version)

    print(f"::set-output name=new_version::{new_version}")
else:
    print("Version pattern not found!")
    sys.exit(1)
