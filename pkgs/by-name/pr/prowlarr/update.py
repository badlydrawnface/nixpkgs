import json
import os
import pathlib
import requests
import shutil
import subprocess
import sys
import tempfile


def replace_in_file(file_path, replacements):
    file_contents = pathlib.Path(file_path).read_text()
    for old, new in replacements.items():
        if old == new:
            continue
        updated_file_contents = file_contents.replace(old, new)
        # A dumb way to check that we’ve actually replaced the string.
        if file_contents == updated_file_contents:
            print(f"no string to replace: {old} → {new}", file=sys.stderr)
            sys.exit(1)
        file_contents = updated_file_contents
    with tempfile.NamedTemporaryFile(mode="w") as t:
        t.write(file_contents)
        t.flush()
        shutil.copyfile(t.name, file_path)


def nix_hash_to_sri(hash):
    return subprocess.run(
        [
            "nix",
            "--extra-experimental-features", "nix-command",
            "hash",
            "to-sri",
            "--type", "sha256",
            "--",
            hash,
        ],
        stdout=subprocess.PIPE,
        text=True,
        check=True,
    ).stdout.rstrip()


nixpkgs_path = "."
attr_path = os.getenv("UPDATE_NIX_ATTR_PATH", "prowlarr")

package_attrs = json.loads(subprocess.run(
    [
        "nix",
        "--extra-experimental-features", "nix-command",
        "eval",
        "--json",
        "--file", nixpkgs_path,
        "--apply", """p: {
          dir = builtins.dirOf p.meta.position;
          version = p.version;
          sourceHash = p.src.src.outputHash;
          yarnHash = p.yarnOfflineCache.outputHash;
        }""",
        "--",
        attr_path,
    ],
    stdout=subprocess.PIPE,
    text=True,
    check=True,
).stdout)

old_version = package_attrs["version"]
new_version = old_version

# Note that we use Prowlarr API instead of GitHub to fetch latest stable release.
# This corresponds to the Updates tab in the web UI. See also
# https://github.com/Prowlarr/Prowlarr/blob/7d813ef97a01af0f36a2beaec32e9cd854fc67f3/src/NzbDrone.Core/Update/UpdatePackageProvider.cs
# https://github.com/Prowlarr/Prowlarr/blob/7d813ef97a01af0f36a2beaec32e9cd854fc67f3/src/NzbDrone.Common/Cloud/ProwlarrCloudRequestBuilder.cs
version_update = requests.get(
    f"https://prowlarr.servarr.com/v1/update/master?version={old_version}&includeMajorVersion=true",
).json()
if version_update["available"]:
    new_version = version_update["updatePackage"]["version"]

if new_version == old_version:
    sys.exit()

source_nix_hash, source_store_path = subprocess.run(
    [
        "nix-prefetch-url",
        "--name", "source",
        "--unpack",
        "--print-path",
        f"https://github.com/Prowlarr/Prowlarr/archive/v{new_version}.tar.gz",
    ],
    stdout=subprocess.PIPE,
    text=True,
    check=True,
).stdout.rstrip().split("\n")

old_source_hash = package_attrs["sourceHash"]
new_source_hash = nix_hash_to_sri(source_nix_hash)

package_dir = package_attrs["dir"]
package_file_name = "package.nix"
deps_file_name = "deps.json"

# To update deps.nix, we copy the package to a temporary directory and run
# passthru.fetch-deps script there.
with tempfile.TemporaryDirectory() as work_dir:
    package_file = os.path.join(work_dir, package_file_name)
    deps_file = os.path.join(work_dir, deps_file_name)

    shutil.copytree(package_dir, work_dir, dirs_exist_ok=True)

    replace_in_file(package_file, {
        # NB unlike hashes, versions are likely to be used in code or comments.
        # Try to be more specific to avoid false positive matches.
        f"version = \"{old_version}\"": f"version = \"{new_version}\"",
        old_source_hash: new_source_hash,
    })

    # We need access to the patched and updated src to get the patched
    # `yarn.lock`.
    patched_src = os.path.join(work_dir, "patched-src")
    subprocess.run(
        [
            "nix",
            "--extra-experimental-features", "nix-command",
            "build",
            "--impure",
            "--nix-path", "",
            "--include", f"nixpkgs={nixpkgs_path}",
            "--include", f"package={package_file}",
            "--expr", "(import <nixpkgs> { }).callPackage <package> { }",
            "--out-link", patched_src,
            "src",
        ],
        check=True,
    )
    old_yarn_hash = package_attrs["yarnHash"]
    new_yarn_hash = nix_hash_to_sri(subprocess.run(
        [
            "prefetch-yarn-deps",
            # does not support "--" separator :(
            # Also --verbose writes to stdout, yikes.
            os.path.join(patched_src, "yarn.lock"),
        ],
        stdout=subprocess.PIPE,
        text=True,
        check=True,
    ).stdout.rstrip())

    replace_in_file(package_file, {
        old_yarn_hash: new_yarn_hash,
    })

    # Generate nuget-to-json dependency lock file.
    fetch_deps = os.path.join(work_dir, "fetch-deps")
    subprocess.run(
        [
            "nix",
            "--extra-experimental-features", "nix-command",
            "build",
            "--impure",
            "--nix-path", "",
            "--include", f"nixpkgs={nixpkgs_path}",
            "--include", f"package={package_file}",
            "--expr", "(import <nixpkgs> { }).callPackage <package> { }",
            "--out-link", fetch_deps,
            "passthru.fetch-deps",
        ],
        check=True,
    )
    subprocess.run(
        [
            fetch_deps,
            deps_file,
        ],
        stdout=subprocess.DEVNULL,
        check=True,
    )

    shutil.copy(deps_file, os.path.join(package_dir, deps_file_name))
    shutil.copy(package_file, os.path.join(package_dir, package_file_name))
