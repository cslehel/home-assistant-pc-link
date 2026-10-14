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
- reading other windows via **UI Automation**;
- starting **PowerShell** from the service while it updates itself (to read
  the download's signatures and to run its installer).

Those are exactly the behaviors this tool is *supposed* to have. The correct
way to keep antivirus happy is **transparency + code signing + an allowlist
entry**, never obfuscation. In fact packing/obfuscating to "hide" from AV makes
detection *more* likely and hurts your SmartScreen reputation.

## What actually reduces false positives

1. **Code-sign the executables.** An unsigned executable from an unknown
   publisher is the most common reason for a false positive, and the
   standalone build is no exception. The build workflow signs `pclink.exe`
   and `pclink-agent.exe` automatically when you add a code-signing
   certificate as two repository secrets: `SIGN_PFX_BASE64` (the `.pfx` file,
   base64-encoded) and `SIGN_PFX_PASSWORD`. To sign by hand:
   ```powershell
   signtool sign /f cert.pfx /p <password> /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 pclink.exe pclink-agent.exe
   ```
   An **OV** certificate builds SmartScreen reputation over time; an **EV**
   certificate gives instant SmartScreen trust.

   Signing also protects the self-update: a signed copy only installs an
   update signed by the same publisher. For that `install.ps1` must be
   signed too and the package must contain a signed catalog of all its files
   (`pclink.cat`); the workflow does both. When signing by hand, do the same
   as described in
   [DEVELOPMENT.md](DEVELOPMENT.md#building-the-standalone-windows-version)
   or the PC will refuse to update itself to that build. See
   [SECURITY.md](SECURITY.md#code-signature-of-an-update).

2. **Build cleanly.** The standalone build already follows the practices that
   keep heuristics calm (`packaging/pclink.spec`): a one-folder build instead of
   a self-extracting single file, no UPX compression, a Windows version
   resource and an icon, and every build is made from public source by
   GitHub Actions, so anyone can check what went into it.

3. **Or run from a Python install.** The Python variant runs the service
   through the official, signed `python.exe`/`pythonservice.exe`, so there is
   no custom executable at all. It is the fallback when a strict antivirus
   will not accept the unsigned standalone build.

4. **Add an allowlist / exclusion** for the install folder on the managed PC
   (the admin does this once). Steps for Microsoft Defender are in
   `defender-exclusion.ps1` (in the zip, and in `packaging/`). Do this only
   on machines you own.

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

For a home setup:

1. Use the standalone version from the Releases page (a signed build if you
   have configured a certificate).
2. Unblock the zip before extracting it, then run `install.cmd`.
3. If your antivirus flags it, run `defender-exclusion.ps1` as administrator
   and report the false positive to the vendor.
4. If a strict antivirus still refuses it, use the Python install instead.
