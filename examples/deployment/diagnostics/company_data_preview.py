"""Render a declared synthetic Office sample after the paid benchmark finishes."""
from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path


from company_agent.services.sandbox import sandbox_worker


RENDER = r'''
import json, subprocess
from pathlib import Path
request = json.loads(Path('/input/request.json').read_text())
suffix = request['suffix']
assert suffix in ('.docx', '.pptx')
profile = Path('/tmp/profile')
(profile / 'user').mkdir(parents=True)
(profile / 'user/registrymodifications.xcu').write_text('<oor:items xmlns:oor="http://openoffice.org/2001/registry"><item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item></oor:items>')
subprocess.run(['libreoffice', '-env:UserInstallation=' + profile.as_uri(),
                '--headless', '--nologo', '--nodefault', '--nofirststartwizard', '--norestore',
                '--convert-to', 'pdf', '--outdir', '/out', '/input/source' + suffix],
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=45)
Path('/out/source.pdf').rename('/out/preview.pdf')
print(json.dumps({'renderer': 'LibreOffice PDF preview'}))
'''

IMAGE = 'ai-gateway/company-eval-preview:20261018'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--case', default='W02', choices=('W01','W02','W03','W04'))
    args = parser.parse_args()
    report = json.loads(args.report.read_text())
    if not report.get('completed_at') or report.get('unrun'):
        parser.error('Paid campaign must finish before rendering local previews.')
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    rows = {(r['case'], r['arm'], r['repeat']): r for r in report['runs']}
    if args.case not in {row['case'] for row in report['runs']}:
        parser.error('Selected case is absent from this completed cohort.')
    results = []
    for arm in ('flash', 'argon'):
        for filename in (('summary.docx', 'briefing.pptx') if args.case == 'W02' else ('summary.docx',)):
            selected = next((rows[(args.case, arm, repeat)] for repeat in (1, 2)
                             if (args.report.parent / rows[(args.case, arm, repeat)]['folder'] / 'downloads' / filename).is_file()), None)
            if selected is None:
                results.append({'case': args.case, 'arm': arm, 'file': filename, 'status': 'artifact_missing'})
                continue
            row = selected
            path = args.report.parent / row['folder'] / 'downloads' / filename
            original = path.read_bytes()
            assert hashlib.sha256(original).hexdigest() == row['artifact_sha256'][filename]
            with zipfile.ZipFile(path) as archive:
                assert not any('vbaproject' in name.lower() for name in archive.namelist())
            item = {'case': args.case, 'arm': arm, 'repeat': row['repeat'], 'file': filename,
                    'primary_passed': row['passed'], 'artifact_sha256': row['artifact_sha256'][filename]}
            try:
                meta, pdf = sandbox_worker(IMAGE, {'suffix': path.suffix},
                    data=original, suffix=path.suffix, output='preview.pdf', entrypoint=('python3', '-c', RENDER), tmpfs_mb=256)
                prefix = f"{arm}-{args.case}-r{row['repeat']}-{path.stem}"
                (args.output / (prefix + '.pdf')).write_bytes(pdf)
                import pymupdf
                images = []
                with pymupdf.open(stream=pdf, filetype='pdf') as doc:
                    meta['pages'] = len(doc)
                    for index, page in enumerate(doc):
                        target = prefix + f'-p{index+1}.png'
                        page.get_pixmap(matrix=pymupdf.Matrix(1.4, 1.4), alpha=False).save(args.output / target)
                        images.append(target)
                item.update(status='rendered', **meta, pdf=prefix + '.pdf', images=images)
            except Exception as error:
                item.update(status='render_inconclusive', error_type=type(error).__name__)
            assert path.read_bytes() == original
            results.append(item)
    value = {'scope': f'{args.case} r1 per arm and format, r2 fallback only when artifact absent; local LibreOffice preview',
             'image': IMAGE,
             'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
             'human_review_performed': False, 'primary_grades_changed': False, 'runs': results}
    (args.output / 'manifest.json').write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(value, ensure_ascii=False))


if __name__ == '__main__':
    main()
