"""Build local OCR fixtures with known text; real document contents stay local."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from pathlib import Path

import pymupdf
from PIL import Image
from PIL import ImageDraw
from PIL import ImageEnhance
from PIL import ImageFilter
from PIL import ImageFont

FONT = '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'


def font(size):
    return ImageFont.truetype(FONT, size, index=3)


def normalize(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text))


def build(destination, repository):
    destination.mkdir(parents=True, exist_ok=True)
    cases = []

    def save(name, image, text, fields, kind, cells=None):
        image_path = destination / (name + '.png')
        image.save(image_path)
        cases.append({
            'id': name, 'kind': kind, 'image': str(image_path),
            'sha256': hashlib.sha256(image_path.read_bytes()).hexdigest(),
            'ground_truth': text, 'critical_fields': fields,
            'table_cells': cells or [],
        })

    lines = [
        '澄川工程股份有限公司 函',
        '地址：臺北市中山區建國北路二段168號7樓',
        '受文者：宏宇營造股份有限公司',
        '發文日期：中華民國115年10月14日',
        '發文字號：澄工字第1150014327號',
        '速別：普通',
        '主旨：辦理工地安全檢查及改善事項，請查照。',
        '說明：',
        '一、訂於115年11月5日09時30分至11時00分召開會議。',
        '二、請於115年10月28日前，提送鷹架及護欄改善報告。',
        '三、改善經費新臺幣876,543元整，請依核定項目辦理。',
        '四、現場應確認安全帽、安全帶及施工圍籬均符合規定。',
        '五、不得將參考資料之日期誤列為本次應辦期限。',
        '附件：工地安全改善清單1份',
        '承辦人：曾婉瑜 電話：(02)2501-6789 分機123',
        '正本：宏宇營造股份有限公司',
        '副本：澄川工程股份有限公司工務部',
    ]
    image = Image.new('RGB', (1654, 2339), 'white')
    draw = ImageDraw.Draw(image)
    for i, line in enumerate(lines):
        draw.text((90, 110 + i * 112), line, font=font(38 if i else 52), fill='black')
    fields = [
        '115年10月14日', '1150014327', '115年11月5日', '09時30分',
        '11時00分', '115年10月28日', '876,543', '曾婉瑜', '2501-6789',
        '澄川工程股份有限公司', '宏宇營造股份有限公司', '臺北市中山區建國北路二段168號7樓',
    ]
    save('synthetic-letter', image, '\n'.join(lines), fields, 'letter')
    degraded = ImageEnhance.Contrast(image).enhance(.55).filter(ImageFilter.GaussianBlur(.7))
    degraded = degraded.rotate(1.7, resample=Image.Resampling.BICUBIC, expand=True, fillcolor='white')
    save('synthetic-letter-scan', degraded, '\n'.join(lines), fields, 'degraded-letter')

    title = '工程材料數量與經費彙整表'
    headers = ['項次', '材料名稱', '數量', '單價（元）', '小計（元）']
    rows = [
        ['1', '熱浸鍍鋅護欄', '12', '1,250', '15,000'],
        ['2', '鋼管施工鷹架', '48', '2,375', '114,000'],
        ['3', '反光施工圍籬', '36', '865', '31,140'],
        ['4', '全身背負安全帶', '17', '3,480', '59,160'],
        ['5', '耐衝擊安全帽', '29', '625', '18,125'],
        ['6', '防墜安全母索', '83', '415', '34,445'],
    ]
    image = Image.new('RGB', (1654, 1900), 'white')
    draw = ImageDraw.Draw(image)
    draw.text((100, 90), title, font=font(52), fill='black')
    draw.text((100, 200), '編號：TC-2026-014　填表日期：115年10月14日', font=font(34), fill='black')
    xs = [90, 220, 800, 990, 1230, 1550]
    top, height = 350, 160
    for x in xs:
        draw.line((x, top, x, top + height * 7), fill='black', width=3)
    for y in range(8):
        draw.line((xs[0], top + height * y, xs[-1], top + height * y), fill='black', width=3)
    for row_index, row in enumerate([headers, *rows]):
        for col, value in enumerate(row):
            draw.text((xs[col] + 16, top + row_index * height + 50), value, font=font(36), fill='black')
    footer = '合計：新臺幣271,870元　覆核：陳韋廷'
    draw.text((100, 1600), footer, font=font(38), fill='black')
    table_text = '\n'.join([
        title, '編號：TC-2026-014 填表日期：115年10月14日',
        *[' '.join(row) for row in [headers, *rows]], footer,
    ])
    cells = [value for row in rows for value in row[1:]]
    save('synthetic-table', image, table_text, ['TC-2026-014', '271,870', '陳韋廷'], 'table', cells)
    save(
        'synthetic-table-scan', image.filter(ImageFilter.GaussianBlur(1.0)).resize((1240, 1425)),
        table_text, ['TC-2026-014', '271,870', '陳韋廷'], 'degraded-table', cells,
    )

    left = [
        '施工安全檢查要點', '第一章 現場管理', '一、進場人員均應配戴安全帽。',
        '二、高處作業應使用全身背負安全帶。', '三、施工圍籬不得任意拆除或移動。',
        '四、臨時電源應裝設漏電斷路器。', '五、工具使用後應歸位並檢查損傷。',
        '六、吊掛作業應設專人指揮。', '七、鷹架應於使用前完成安全查驗。',
    ]
    right = [
        '第二章 異常改善', '八、發現缺失應立即停止危險作業。',
        '九、改善期限為115年10月28日。', '十、複查日期為115年11月2日。',
        '十一、改善報告應附施工前後照片。', '十二、紀錄編號為QA-2026-0198。',
        '十三、確認責任單位與承辦人。', '十四、全部完成後始得恢復作業。', '主管簽核：許庭瑄',
    ]
    image = Image.new('RGB', (1800, 1700), 'white')
    draw = ImageDraw.Draw(image)
    for x, column in ((70, left), (950, right)):
        for i, line in enumerate(column):
            draw.text((x, 100 + i * 150), line, font=font(31), fill='black')
    draw.line((900, 80, 900, 1570), fill='gray', width=2)
    save(
        'synthetic-columns', image, '\n'.join(left + right),
        ['115年10月28日', '115年11月2日', 'QA-2026-0198', '許庭瑄'], 'two-columns',
    )

    lines = [
        '小字與易混淆字元辨識測試', '案件編號：O0I1-L5S8-B6G9-2026',
        '承辦人：龔韋翰；覆核人：鍾毓璇；聯絡人：鄒鈺婷',
        '地點：臺中市烏日區榮泉路258巷16弄3號',
        '保固期間：115年12月31日至118年12月30日',
        '採購金額：NT$1,234,567.89；稅率：5%；數量：0.75公噸',
        '識別代碼：AB-01928-ZX；版本：R2.07；設備：IP68',
        '注意：釐清權責、核實數據、逐項複查，勿擅自變更內容。',
    ]
    lines += [f'檢核項目{n:02d}：確認材料規格、施工位置與改善照片；紀錄碼C{n:03d}。' for n in range(1, 15)]
    image = Image.new('RGB', (1654, 1900), 'white')
    draw = ImageDraw.Draw(image)
    for i, line in enumerate(lines):
        draw.text((70, 80 + i * 75), line, font=font(25), fill='black')
    save(
        'synthetic-small-print', image, '\n'.join(lines),
        [
            'O0I1-L5S8-B6G9-2026', '龔韋翰', '鍾毓璇', '鄒鈺婷',
            '1,234,567.89', '0.75', 'AB-01928-ZX', 'R2.07', 'IP68',
        ], 'small-print',
    )

    real_names = [
        'fb431d7d-9228-4240-b939-20a59b171d69.pdf',
        '7f45c447-c2ae-4b98-b1bb-f5473e8c22ea.pdf',
        '劦字第1150013326號.pdf', '劦字第1150013525號.pdf',
    ]
    for index, name in enumerate(real_names, 1):
        with pymupdf.open(repository / 'example_files' / name) as doc:
            page = doc[0]
            text = page.get_text()
            normalized = normalize(text)
            fields = sorted(set(re.findall(r'\d{2,3}年\d{1,2}月\d{1,2}日|\d{8,12}', normalized)))
            # Annotate both the complete document number and its receiver,
            # rather than treating the numeric suffix as the entire identifier.
            for pattern in (r'發文字號[:：]([^:：]{1,80}?號)', r'受文者[:：](.{2,80}?)發文日期'):
                match = re.search(pattern, normalized)
                if match:
                    fields.append(match[1])
            pix = page.get_pixmap(dpi=150)
            image = Image.frombytes('RGB', [pix.width, pix.height], pix.samples)
            save(f'real-{index:02d}', image, text, fields, 'real-text-backed-letter')
            if index <= 2:
                scan = ImageEnhance.Contrast(image).enhance(.6).filter(ImageFilter.GaussianBlur(.6))
                scan = scan.rotate(-1.3, resample=Image.Resampling.BICUBIC, expand=True, fillcolor='white')
                save(f'real-{index:02d}-scan', scan, text, fields, 'degraded-real-letter')
    manifest = {'cases': cases, 'ground_truth_source': 'Synthetic source text or PDF embedded text; never another OCR output.'}
    (destination / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    print(f'Prepared {len(cases)} cases; real text and outputs remain in {destination}.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--repository', type=Path, default=Path(__file__).resolve().parents[3])
    args = parser.parse_args()
    build(args.destination, args.repository)
