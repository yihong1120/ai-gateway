"""Synthetic workbook shared by evaluation scenarios and office regressions."""
import io

from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference


def workbook():
    b = Workbook()
    s = b.active
    s.title = '工程'
    s.append(['項目', '數量'])
    s.append(['A', 10])
    s.append(['B', 20])
    s.append(['總計', '=SUM(B2:B3)'])
    s['B2'].number_format = '0.00'
    s['B3'].number_format = '0%'
    s.freeze_panes = 'B2'
    s.column_dimensions['A'].width = 24
    s.merge_cells('D1:E1')
    s['D1'] = '合併'
    t = b.create_sheet('第二表')
    t['A1'] = '原值'
    chart = BarChart()
    chart.add_data(Reference(s, min_col=2, min_row=2, max_row=3))
    s.add_chart(chart, 'G2')
    out = io.BytesIO()
    b.save(out)
    return out.getvalue()

