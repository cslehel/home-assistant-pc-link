Home Assistant PC Link - standalone Windows build (no Python needed)
=====================================================================

INSTALL
 1. Before extracting: right-click the downloaded zip > Properties >
    tick "Unblock" > OK. (Otherwise Windows warns about every file.)
 2. Extract the zip and double-click install.cmd. Allow the administrator prompt.
 3. The first run creates C:\ProgramData\PCLink\config.yaml, with the
    connection to Home Assistant already set up, and opens it in Notepad.
    Enter your users, save it, then double-click install.cmd again.
    The program is copied to C:\Program Files\PCLink and the service starts;
    the installer ends by showing a token.
 4. In Home Assistant (with the PC Link integration installed): Settings ->
    Devices & services. The PC shows up under "Discovered": click Add and
    enter the token. More:
    https://github.com/cslehel/home-assistant-pc-link/blob/main/docs/HOMEASSISTANT.md

UPGRADE
    Extract the new version and double-click install.cmd. Settings and
    statistics are kept.

UNINSTALL
    Double-click uninstall.cmd. Managed users get their logon rights back.
    Settings and statistics stay in C:\ProgramData\PCLink; to delete them too,
    run in PowerShell:  .\uninstall.ps1 -Purge

COMMANDS (from an administrator prompt)
    "C:\Program Files\PCLink\pclink.exe" help
    "C:\Program Files\PCLink\pclink.exe" check-config
    "C:\Program Files\PCLink\pclink.exe" setup-api --show    (the token again)
    "C:\Program Files\PCLink\pclink.exe" selftest

ANTIVIRUS
    If your antivirus flags the program, see
    https://github.com/cslehel/home-assistant-pc-link/blob/main/docs/ANTIVIRUS.md
    defender-exclusion.ps1 adds a Microsoft Defender exclusion (run as administrator).
