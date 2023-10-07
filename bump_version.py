import re
import sys

with open("setup.py", "r") as f:
    content = f.read()

# Extract the current version
match = re.search(r'version="([\d.]+)-beta\.\d+"', content)
if match:
    version = match.group(1)
    parts = version.split('.')
    # Increment the patch version number (the third number in the version)
    parts[-1] = str(int(parts[-1]) + 1)
    new_main_version = '.'.join(parts)
    # Reset beta version to 1
    new_version = f"{new_main_version}-beta.1"

    content = content.replace(f'version="{version}"', f'version="{new_version}"')

    with open("setup.py", "w") as f:
        f.write(content)

    # Output the new version for subsequent GitHub Actions steps
    print(f"::set-output name=new_version::{new_version}")
else:
    print("Version pattern not found!")
    sys.exit(1)
