import re
import sys

with open("setup.py", "r") as f:
    content = f.read()

# Extract the current version
match = re.search(r'version="([\d.]+-beta\.\d+)"', content)
if match:
    version = match.group(1)
    main_version, beta_number = re.match(r"([\d.]+)-beta\.(\d+)", version).groups()
    new_beta_number = str(int(beta_number) + 1)
    new_version = f"{main_version}-beta.{new_beta_number}"

    content = content.replace(f'version="{version}"', f'version="{new_version}"')

    with open("setup.py", "w") as f:
        f.write(content)

    # Output the new version for subsequent GitHub Actions steps
    print(f"::set-output name=new_version::{new_version}")
else:
    print("Version pattern not found!")
    sys.exit(1)
