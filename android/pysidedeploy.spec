[app]

# Title of your application
# Used by pyside6-android-deploy as the p4a dist/package base name, so it must
# stay ASCII. The Cyrillic launcher label ("Контролька") and the ru.shotsync.ctrlka
# application id are applied to the generated buildozer.spec by
# android_deploy_pinned.py.
title = ctrlka

# Project root directory. Default: The parent directory of input_file
project_dir = .

# Source file entry point path. Default: main.py
input_file = main.py

# Directory where the executable output is generated
exec_directory =

# Path to the project file relative to project_dir
project_file =

# Application icon (relative to the staged build dir, i.e. build/rawww/...)
icon = rawww/assets/ctrlka-icon.png

[python]

# Python path
python_path =

# Python packages to install
packages = Nuitka==4.0

# Buildozer: for deploying Android application
android_packages = buildozer==1.5.0,cython==0.29.33

[qt]

# Paths to required QML files. Comma separated
# Normally all the QML files required by the project are added automatically
# Design Studio projects include the QML files using Qt resources
qml_files =

# Excluded qml plugin binaries
excluded_qml_plugins =

# Qt modules used. Comma separated
modules = Core,Gui,Widgets,Network,WebSockets,Sql

# Qt plugins used by the application. Only relevant for desktop deployment
# For Qt plugins used in Android application see [android][plugins]
plugins =

[android]

# Path to PySide wheel
wheel_pyside =

# Path to Shiboken wheel
wheel_shiboken =

# Plugins to be copied to libs folder of the packaged application. Comma separated
# Setting this overrides pyside6-android-deploy's auto-detection, so the platform
# plugin must be listed explicitly. The TLS backend is required for HTTPS/WSS
# (shotsync.ru login) but is loaded at runtime, so auto-detection misses it.
plugins = platforms_qtforandroid,tls_qopensslbackend,tls_qcertonlybackend

[nuitka]

# Usage description for permissions requested by the app as found in the Info.plist file
# of the app bundle. Comma separated
# eg: NSCameraUsageDescription:CameraAccess
macos.permissions =

# Mode of using Nuitka. Accepts standalone or onefile. Default: onefile
mode = onefile

# Specify any extra nuitka arguments
# eg: extra_args = --show-modules --follow-stdlib
extra_args = --quiet --noinclude-qt-translations

[buildozer]

# Build mode
# Possible values: [release, debug]
# Release creates a .aab, while debug creates a .apk
mode = debug

# Path to PySide6 and shiboken6 recipe dir
recipe_dir =

# Path to extra Qt Android .jar files to be loaded by the application
jars_dir =

# If empty, uses default NDK path downloaded by buildozer
ndk_path =

# If empty, uses default SDK path downloaded by buildozer
sdk_path =

# Other libraries to be loaded at app startup. Comma separated.
local_libs =

# Architecture of deployed platform
# Possible values: ["aarch64", "armv7a", "i686", "x86_64"]
arch = aarch64
