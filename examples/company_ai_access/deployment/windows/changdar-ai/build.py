"""Build the AnyCPU Windows EXE with exactly the reviewed signed PS1 payload."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import zipfile


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--signed-scripts',type=Path,required=True)
    parser.add_argument('--sha256',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if hashlib.sha256(args.signed_scripts.read_bytes()).hexdigest()!=args.sha256:
        raise ValueError('Windows signed upload hash mismatch.')
    if args.output.exists(): raise ValueError('Use a new immutable output directory.')
    args.output.mkdir(parents=True)
    scripts={'Setup-CompanyAI.ps1','Get-CompanyAIKey.ps1','Start-CompanyCodex.ps1'}
    with tempfile.TemporaryDirectory(prefix='changdar-ai-payload-') as temp:
        payload=Path(temp)
        with zipfile.ZipFile(args.signed_scripts) as z:
            if len(z.namelist())!=6 or set(z.namelist())!=scripts|{'RootCA.cer','Publisher.cer','release.json'}:
                raise ValueError('Unexpected signed input archive.')
            manifest=json.loads(z.read('release.json'))
            for e in manifest['files']:
                if e['name'] not in scripts or hashlib.sha256(z.read(e['name'])).hexdigest()!=e['sha256']:
                    raise ValueError('Signed script manifest mismatch.')
            files={n:z.read(n) for n in scripts|{'RootCA.cer','Publisher.cer'}}
        for n,d in files.items(): (payload/n).write_bytes(d)
        (payload/'payload-hashes.json').write_text(json.dumps({n:hashlib.sha256(d).hexdigest() for n,d in files.items()}))
        subprocess.run(['dotnet','build',str(Path(__file__).with_name('ChangdarAI.csproj')),
                        '-c','Release','-p:PayloadDirectory='+str(payload),'-o',str(args.output),'--nologo'],check=True)
    exe=args.output/'changdar-ai-setup.exe'
    digest=hashlib.sha256(exe.read_bytes()).hexdigest()
    settings={'version':'1.3.0','unsigned_sha256':digest,
              'root_sha256':'338C3415CB3E60F67A7E50010E8D65EA2A30EF634F6C0A3009D391C86EC7BFFF',
              'publisher_sha256':'CCE5A188EC6E254BDFE3975645F5270C1F506F48491BC8B2F4B5C1D42A7CD24D',
              'signer_thumbprint':'65715DD7C6D96C39ED7F01918C9ADAA783A12BEE'}
    archive=args.output/'changdar-ai-installer-input.zip'
    with zipfile.ZipFile(archive,'x',compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr('changdar-ai-setup.exe',exe.read_bytes())
        z.writestr('signing-input.json',json.dumps(settings,indent=2))
        z.writestr('Sign-ChangdarAIInstaller.ps1',Path(__file__).with_name('Sign-ChangdarAIInstaller.ps1').read_bytes())
    print(json.dumps({'unsigned_exe_sha256':digest,'signing_input':str(archive),
                      'signing_input_sha256':hashlib.sha256(archive.read_bytes()).hexdigest()},indent=2))


if __name__=='__main__':main()
