#!/usr/bin/env python3
"""Build-time preparation for the Expo SDK 39 bare Android APK build.

This app is an Expo managed project (SDK 39 / RN 0.63). To get a standalone
.apk we eject it to the bare workflow inside CI, then build with Gradle.

Two passes:
  appconfig  -> before `expo eject`: give app.json the identifiers eject needs
                and drop the OTA `updates` block (there is no update server).
  native     -> after `expo eject`: patch the *generated* android/ project for
                an arm64-v8a only release APK that loads its bundled JS.

Nothing in the app source (src/, App.tsx) is touched.
"""

import json
import pathlib
import re
import sys
from typing import NoReturn

ROOT = pathlib.Path(__file__).resolve().parents[2]
ANDROID = ROOT / "android"
APP_ID = "com.sanjib.messagesclone"


def fail(msg) -> NoReturn:
    sys.exit(f"prepare_android: {msg}")


def read(path):
    p = pathlib.Path(path)
    if not p.exists():
        fail(f"missing {p}")
    return p.read_text()


def write(path, text):
    pathlib.Path(path).write_text(text)


def replace_once(text, old, new, label):
    if text.count(old) != 1:
        fail(f"expected exactly one {label!r} in target file, found {text.count(old)}")
    return text.replace(old, new)


# --------------------------------------------------------------------------- #
# pass 1: app.json
# --------------------------------------------------------------------------- #
def appconfig():
    path = ROOT / "app.json"
    config = json.loads(read(path))
    expo = config.setdefault("expo", {})

    # eject --non-interactive refuses to run without these two ids.
    expo.setdefault("android", {})["package"] = APP_ID
    expo.setdefault("ios", {})["bundleIdentifier"] = APP_ID

    # No OTA backend exists for a sideloaded APK: the JS bundle is embedded.
    expo.pop("updates", None)

    write(path, json.dumps(config, indent=2) + "\n")
    print(f"app.json: android.package={APP_ID}, ios.bundleIdentifier={APP_ID}, updates=off")


# --------------------------------------------------------------------------- #
# pass 2: generated native project
# --------------------------------------------------------------------------- #
def drop_expo_updates():
    # 1. dependency (installed by eject even with no `updates` config)
    pkg_path = ROOT / "package.json"
    pkg = json.loads(read(pkg_path))
    removed = pkg.get("dependencies", {}).pop("expo-updates", None)
    write(pkg_path, json.dumps(pkg, indent=2) + "\n")
    print(f"package.json: expo-updates dependency removed ({removed})")

    # 2. gradle hook that builds an OTA manifest at assemble time
    gradle_path = ANDROID / "app" / "build.gradle"
    gradle = read(gradle_path)
    hook = 'apply from: "../../node_modules/expo-updates/scripts/create-manifest-android.gradle"\n'
    if hook in gradle:
        gradle = gradle.replace(hook, "")
        write(gradle_path, gradle)
        print("app/build.gradle: expo-updates manifest hook removed")

    # 3. manifest metadata read by UpdatesController
    manifest_path = ANDROID / "app" / "src" / "main" / "AndroidManifest.xml"
    lines = read(manifest_path).splitlines(keepends=True)
    kept = [ln for ln in lines if "expo.modules.updates" not in ln]
    if len(kept) != len(lines):
        write(manifest_path, "".join(kept))
        print(f"AndroidManifest.xml: {len(lines) - len(kept)} expo-updates meta-data line(s) removed")

    # 4. MainApplication.java: release builds load UpdatesController's bundle
    #    file, which cannot exist without an update server. Fall back to the
    #    index.android.bundle embedded by react.gradle.
    java_path = next((ANDROID / "app" / "src" / "main" / "java").rglob("MainApplication.java"))
    java = read(java_path)
    if "UpdatesController" in java:
        java = java.replace("import expo.modules.updates.UpdatesController;\n", "")
        java = re.sub(
            r"\n    @Override\n    protected @Nullable String getJSBundleFile\(\) \{.*?\n    \}\n",
            "\n",
            java,
            count=1,
            flags=re.S,
        )
        java = re.sub(
            r"\n    @Override\n    protected @Nullable String getBundleAssetName\(\) \{.*?\n    \}\n",
            "\n",
            java,
            count=1,
            flags=re.S,
        )
        java = java.replace(
            "    if (!BuildConfig.DEBUG) {\n      UpdatesController.initialize(this);\n    }\n\n",
            "",
        )
        if "UpdatesController" in java:
            fail("UpdatesController reference still present in MainApplication.java")
        write(java_path, java)
        print(f"{java_path.name}: UpdatesController wiring removed")


def configure_abi_and_repos():
    gradle_path = ANDROID / "app" / "build.gradle"
    gradle = read(gradle_path)

    # arm64-v8a only: one small APK instead of four per-ABI splits.
    # Inserted as the last entry of the android.defaultConfig block.
    if "abiFilters" not in gradle:
        lines = gradle.splitlines(keepends=True)
        try:
            start = next(i for i, ln in enumerate(lines) if "defaultConfig {" in ln)
        except StopIteration:
            fail("defaultConfig block not found in app/build.gradle")
        end = next((i for i in range(start + 1, len(lines)) if lines[i].strip() == "}"), None)
        if end is None:
            fail("could not find end of defaultConfig block")
        lines.insert(end, '        ndk {\n            abiFilters "arm64-v8a"\n        }\n')
        gradle = "".join(lines)
    write(gradle_path, gradle)
    print("app/build.gradle: abiFilters -> arm64-v8a")

    # jcenter is read-only/eol; mavenCentral first keeps artifact resolution
    # working when jcenter is unavailable.
    root_gradle = ANDROID / "build.gradle"
    text = read(root_gradle)
    text = text.replace("        google()\n        jcenter()", "        google()\n        mavenCentral()\n        jcenter()")
    text = text.replace("https://www.jitpack.io", "https://jitpack.io")
    write(root_gradle, text)

    props_path = ANDROID / "gradle.properties"
    props = read(props_path)
    if not re.search(r"^org\.gradle\.jvmargs=", props, flags=re.M):
        props = props.rstrip("\n") + "\n\norg.gradle.jvmargs=-Xmx3072m -XX:MaxMetaspaceSize=1024m -Dfile.encoding=UTF-8\n"
        write(props_path, props)
        print("gradle.properties: jvmargs set for the release build")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "appconfig":
        appconfig()
    elif mode == "native":
        if not ANDROID.is_dir():
            fail("android/ not found - run `expo eject` first")
        drop_expo_updates()
        configure_abi_and_repos()
    else:
        fail("usage: prepare_android.py {appconfig|native}")


if __name__ == "__main__":
    main()
