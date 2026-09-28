# Antivirus, SmartScreen & false positives

You asked to make sure the app "will not trigger any antivirus software." Here
is an honest, practical answer.

## The honest part

No one can *guarantee* zero antivirus detections, and any claim otherwise is
false. This program legitimately performs actions that behavioral/heuristic
engines watch closely:

- launching a process into **another user's session** with their token
  (`CreateProcessAsUser`);
- **logging users off** and changing account **logon hours**
  (`NetUserSetInfo`, `net.exe user /times`);
- running as a **LocalSystem service** that **resists being stopped**;
- reading other windows via **UI Automation**.

Those are exactly the behaviors this tool is *supposed* to have. The correct
way to keep antivirus happy is **transparency + code signing + an allowlist
entry**, never obfuscation. In fact packing/obfuscating to "hide" from AV makes
detection *more* likely and hurts your SmartScreen reputation.

## What actually reduces false positives

1. **Run from a Python install, not a bare PyInstaller `.exe`.** Unsigned,
   single-file PyInstaller binaries are the single most common cause of AV
   false positives. Running `python -m pclink ...` from an official Python
   installation avoids that entirely and is the recommended deployment here.

2. **Code-sign everything you ship.** If you build an `.exe`/installer, sign it
   with an **Authenticode** certificate:
   ```powershell
   signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 dist\pclink.exe
   ```
   An **OV** certificate builds SmartScreen reputation over time; an **EV**
   certificate gives instant SmartScreen trust. Sign both the executable and
   the installer.

3. **Build cleanly** if you use PyInstaller: prefer a one-folder build over
   one-file, don't use UPX compression (`--noupx`), and sign the result.
   A ready spec is in `packaging/pclink.spec`.

4. **Add an allowlist / exclusion** for the install folder on the managed PC
   (the admin does this once). Steps for Microsoft Defender are in
   `packaging/defender-exclusion.ps1`. Do this only on machines you own.

5. **Submit false-positive reports** to any vendor that flags a signed build:
   - Microsoft: https://www.microsoft.com/wdsi/filesubmission
   - Others have similar "submit a false positive" forms.

6. **Keep it open-source and reproducible.** A public repo + signed releases
   from a known publisher is what turns "unknown binary" into "trusted app."

## What this project does *not* do

It does not obfuscate, pack, encrypt strings, hollow processes, disable
Defender, or hide from the process list to evade detection. Those techniques
would (a) increase detections, (b) destroy your code-signing reputation, and
(c) be user-hostile on your own machine. Tamper resistance is done with
supported Windows service ACLs instead — see [SECURITY.md](SECURITY.md).

## Recommended deployment

For a home setup, the lowest-friction and lowest-false-positive path is:

1. Install official Python for all users.
2. `pip install -r requirements-windows.txt`.
3. `python -m pclink install`.
4. Add a Defender exclusion for the install directory.

No custom binary, nothing to sign, nothing for SmartScreen to distrust.
