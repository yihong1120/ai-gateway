# Company AI internal signing

This deployment uses B for a private, Code Signing-only root CA and public CRL
publication. The CHANGDAR Windows 11 VM (bridge IP 192.168.1.98) owns its
non-exportable machine signing key. It signs and verifies the three production
scripts using Windows Authenticode. The legacy employee PS1 never installs trust.
The changdar-ai EXE can provision CurrentUser trust only after the user explicitly
agrees in its UI; no administrator or machine-wide trust change is required.

## changdar-ai EXE (1.2.3 managed model launch)

The main download is `/company-ai/downloads/changdar-ai-setup.exe`. Policy adds
`product_name: changdar-ai` and `windows_installer_url`; the existing PS1 URL remains
available for IT. Flutter handoff: `docs/company-ai-access-flutter-handoff.md`.

The AnyCPU .NET Framework 4.8 WinForms executable embeds the three signed scripts,
public root/publisher and a hash manifest. Windows 11 already includes the runtime;
Windows ARM64 needs .NET Framework 4.8.1 and still requires its own device acceptance.
The EXE uses asInvoker and rejects elevated interactive installation so enrollment,
DPAPI and settings stay with the employee. It preserves the existing CompanyAI data
directory and provider ID while updating product and provider display names.

On Start, after the explicit trust checkbox, it checks resource hashes and pinned
public certificates, creates a per-user ACL-protected extraction directory, adds
only the pinned CurrentUser root, requires online chain/revocation validation and
WinVerifyTrust verification of its own PE and every PS1, then adds the publisher.
New trust entries are removed if validation fails. Setup then runs with process-only
RemoteSigned for compatibility with official vendor installers. No persistent
CurrentUser/LocalMachine execution policy is edited; enforced GPO retains precedence.
Signed company credential helpers use process-only AllSigned so fresh accounts
with the Windows default Restricted can still authenticate after installation.

The UI defaults to both tools, shows live setup output, keeps provider replacement
unchecked, and requires the existing explicit browser/code approval. Certificate
fingerprints are visible before consent. Internal-CA signing does not eliminate
SmartScreen reputation or initial unknown-publisher prompts.

Version 1.2.3 fixes trusted home/project config overriding the managed default.
The Windows `codex` entry is a small CMD launcher that invokes the signed company
PowerShell launcher and then the pinned native Codex binary. Every invocation
selects the managed home and passes model/provider/catalog/daemon defaults ahead
of user arguments. The saved managed choice is retained among the three approved
Gemini models, and explicit `-m` still works. Personal config is left intact.
Installer discovery skips its own CMD entry to avoid recursive installation.
Existing devices can use `-Tools Codex -RefreshModelsOnly`; this also upgrades
the signed launcher without a new browser approval. The managed command directory
is moved to the front of User/process PATH while preserving all other entries;
this also repairs older installations whose native binary precedes the launcher.

Version 1.2.1 adds bounded retries for transient download/read/poll failures,
a public-only progress file, approval code/countdown/link buttons, actionable
failure categories, retry with a fresh run and safe cancellation. Once a key is
delivered, configuration and ACK finish without forced process termination.
A per-account mutex prevents duplicate EXE instances. No automatic approval or
unbounded retry is performed. Browser approval remains valid for ten minutes;
a successful approval gets a separate, bounded five-minute activation window,
including when the next native poll occurs just after browser-code expiry.

`--ui-self-test REPORT.json` exercises countdown/link validation and all error
categories without enrollment, trust changes or model calls. Fault injection in
`tests/windows/Test-ChangdarAIRecovery.ps1` verifies transient recovery, retry
limits, permanent HTTP denials, cancellation and redacted status output.

Build from the separately reviewed, Windows-signed script ZIP:

```bash
python examples/company_ai_access/deployment/windows/changdar-ai/build.py --signed-scripts SIGNED_SCRIPTS.zip --sha256 VM_REPORTED_SHA256 --output NEW_OUTPUT_DIRECTORY
```

Transfer the pinned signing-input ZIP only to the signing VM, run its
`Sign-ChangdarAIInstaller.ps1`, compare the returned artifact hash and independently
verify on Linux. `signed_release.py` accepts `--source-version`, `--windows-version`,
`--installer`, `--unsigned-installer` and independently recorded hashes. It permits
only PE checksum/security-directory changes and appended Authenticode certificates;
reviewed script content must also remain identical. All four files require valid
signatures, timestamps, CRLs and the pinned root/leaf before release packaging.

`--self-test REPORT.json` validates resource hashes and Windows signatures without
trust changes, tool installation or enrollment. The signing VM passed this test,
normal Explorer GUI launch and a direct invocation of the same CurrentUser trust
routine. Full employee CLI installation through the GUI and ARM64 are separate
acceptance checks. VM-wide signing trust was already installed for native signing.

Previous production source 1.1.9 was signed and deployed. All three downloaded scripts were
Valid on CHANGDAR, and the downloaded setup loaded successfully in an AllSigned
child PowerShell process using `-?` (no enrollment or CLI installation). Linux
independently verified their signatures, timestamps, pinned leaf/root and CRLs.
See `docs/company-ai-internal-signing-production-validation-2026-10-24.json`.
The previous trust-bootstrap toolkit was 1.0.1; the current PS1 signing toolkit is 1.1.3 for source 1.2.3. IT's public-certificate-only distribution
bundle is `examples/deployment/archive/releases/company-ai-trust-20261024.zip`; machine-wide employee trust deployment
is not performed by the installer.

B's root operator can use libvirt keyboard/screenshot console management while
the approved VM administrator PowerShell is open. VM SSH, WinRM and a QEMU guest
agent are not required or enabled. Console key injection needs the English input
mode and sufficient key-release delays. Confirm the foreground terminal before
typing and avoid concurrent user input. The VM's hosts mapping directs the portal
to B's 192.168.1.88, preserving its HTTPS hostname/certificate validation and avoiding
router hairpin SNAT. The original hosts file is backed up beside it as
`hosts.company-ai-before-20261024`. Company Nginx allowlists remain unchanged.

## First enrollment

1. B's operator installs `internal_ca.py` with `install_internal_ca.py`. Only root
   may read the CA key, password and registry. The public CA has a critical Code
   Signing EKU, a path length of zero and no permission to issue TLS certificates.
2. On CHANGDAR, open PowerShell as administrator with `-NoProfile`. All execution
   policy scopes were Undefined, so the effective Windows-client default was
   Restricted. Explicitly configure this signing VM account to the company-approved
   RemoteSigned policy before running the reviewed signing toolkit. This is a VM
   provisioning operation; no employee policy is changed by the tools.
3. Download `https://changdar-server.mooo.com/company-ai/signing/CompanyAI-SigningTools.zip`,
   compare its SHA256 with the operator's independently supplied value, extract it
   and unblock only the reviewed toolkit scripts. Run `New-CompanySigningRequest.ps1`.
   It creates an RSA3072 machine key with both export policies disabled, keeps the
   key in Windows CNG, and PUTs only the public CSR to B. Re-running retains the key.
4. Return the printed `csr_sha256` to B's operator. The root-only CA CLI issues only
   the matching request, requiring the dedicated subject, valid signature and
   signing-only constraints:

   ```bash
   /usr/bin/python3 /opt/company-ai-code-signing/internal_ca.py issue \
     --csr /var/lib/company-ai-signing-incoming/request.csr \
     --expected-sha256 HASH_REPORTED_BY_THE_VM
   ```

5. Run `Complete-CompanySigning.ps1` in the same VM account/toolkit. It checks the
   pinned root, exact CSR identity and issuer/leaf constraints. It provisions only
   the pinned root first so Windows can retrieve its CRL, then requires full online
   revocation validation before trusting the publisher. A newly added root is removed
   if this validation fails. It accepts the issued public certificate
   against the existing non-exportable key, signs all three reviewed scripts with
   SHA256 and a timestamp, and verifies each signature with Windows. Only public
   certificates, manifest and signed scripts are uploaded to B. Return the printed
   signed ZIP hash to the operator. A signed ZIP is not automatically published.
6. B's operator verifies the exact returned upload/hash, approved unsigned source,
   pinned publisher and Windows verification result before preparing and deploying
   an immutable signed source release. Keep the existing unsigned download until
   all checks pass. No credential registry or model policy change is required.

Toolkit 1.1.3 sources are the exact reviewed production 1.2.3 PS1 bytes, including the
production Portal, default installation of both tools, daemon workaround and managed
Codex defaults with the preferred command PATH.
Never change source, encoding, portal or line endings after signing.

## Employee trust provisioning

IT deploys the public root into Trusted Root Certification Authorities and the
public publisher into Trusted Publishers using the existing endpoint-management
process. `Install-CompanyAITrust.ps1` is a separate, explicit IT tool, not part of
normal employee setup. It requires independently obtained root/publisher SHA256
pins, validates Code Signing-only scope, and installs only the pinned root before
requiring online revocation validation and publisher trust. A newly added root is
removed on failure. It defaults to CurrentUser and supports LocalMachine for IT.
It never changes execution policy or exports/imports a private key. Bootstrap
trust must be provisioned through an already authorized IT channel; signing does
not make an untrusted private CA automatically trusted.

Windows acceptance must use the employee's intended execution policy and company
trust. Restricted blocks all PS1 files even with a valid signature. RemoteSigned
or AllSigned must be chosen through company policy; the employee installer does
not change it. This internal publisher is not publicly trusted for outside users.

## CA operations and revocation

`company-ai-code-signing-crl.timer` refreshes a seven-day CRL daily on B without
network access. Monitor this timer and `pki.json`'s CRL expiry. Do not allow stale
CRLs to be treated as success and do not disable online revocation checks.

```bash
systemctl status company-ai-code-signing-crl.timer company-ai-code-signing-crl.service
python3 /opt/company-ai-code-signing/internal_ca.py refresh-crl
python3 /opt/company-ai-code-signing/internal_ca.py revoke --serial CERTIFICATE_SERIAL
```

Revoked keys cannot be reissued with the same CSR. A replacement signing key must
be generated explicitly. Existing clients can cache a still-valid CRL; for an
incident, IT must also distribute publisher distrust/removal or an appropriate
cache-refresh policy. Publication alone is not an instant revocation guarantee.

Root private material is under `/var/lib/company-ai-code-signing` (0700; files
0600), with root-private backups under `/var/backups/company-ai-code-signing`.
Keep an offline protected backup; never put these directories in source artifacts,
web roots, the VM toolkit or employee trust packages. The public directory contains
only the root CER, publisher CER, CRL and public metadata. No system TLS trust is
changed on B. Reinitialization preserves the CA; partial state requires operator
recovery rather than a newly generated identity.

The two Nginx upload locations accept PUT only, from the signing VM /32 and local
operator loopback, with 16 KiB/2 MiB limits. There is no HTTP CA-signing endpoint.
Public PKI/toolkit downloads inherit the live company download network allowlist.
If VM IP changes, update only its explicitly scoped upload rule after verification.

## Rollback

Restore the root-private saved Nginx snippet, check `nginx -t`, then reload Nginx.
Disable the CRL timer only when the PKI is fully retired: previously distributed
certificates still depend on its CRL endpoint. Preserve CA state and signed
artifacts for audit. IT removes only the recorded root/publisher fingerprints from
the selected Windows trust stores when retiring this authority. Never remove other
certificates or restore revoked keys by rolling back registry state.
