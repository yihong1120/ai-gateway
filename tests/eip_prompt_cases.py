"""Held-out business inputs: never interpolated into production instructions."""
from __future__ import annotations


def letter(project, subject, body):
    return '\n'.join((
        '工程管理中心 函',
        '受文者：維運團隊',
        '發文日期：中華民國116年3月1日',
        '發文字號：工管字第1160301001號',
        '主旨：'+subject,
        '工程名稱：'+project,
        body,
    ))


CASES = [
    {
        'name': 'meeting_minutes_received_after_event', 'mode': 'dated', 'dates': ['2026/09/08'],
        'times': ['10:00'], 'end_dates': ['2026/09/08'], 'end_times': ['11:00'],
        'sources': [
            letter(
                '東河交流道側車道工程', '號誌、路燈遷移及防汛道路封閉會勘紀錄。',
                '會勘紀錄\n一、時間：115年9月8日（星期二）上午10時00分\n二、地點：工程東側起點鄰道路路口\n五、結論：號誌及路燈依現況調整；防汛道路維持封閉。\n六、散會（上午11:00）',
            ),
        ],
    },
    {
        'name': 'application_submission_deadline', 'mode': 'dated', 'dates': ['2027/10/14'],
        'sources': [
            letter(
                '翠湖泵站更新工程', '申請資料繳交時程。',
                '請於116年10月14日前提出使用許可申請，核准後始得啟用設備。',
            ),
        ],
    },
    {
        'name': 'application_extension_pending', 'mode': 'review',
        'sources': [
            letter(
                '翠湖泵站更新工程', '申請資料繳交時程。',
                '本單位申請將使用許可資料繳交日調至116年10月14日；對方尚未答覆。',
            ),
        ],
    },
    {
        'name': 'procedure_without_keyword', 'mode': 'dated', 'dates': ['2027/11/19'],
        'sources': [
            letter(
                '霧嶺通風改善工程', '資格審議作業。',
                '資格審議所需文件的收件截止日為116年11月19日，請維運團隊送達；資格結果後續另行通知。',
            ),
        ],
    },
    {
        'name': 'extension_confirmed_in_attachment', 'mode': 'dated', 'dates': ['2027/10/14'],
        'sources': [
            letter(
                '翠湖泵站更新工程', '申請資料繳交時程。',
                '本單位申請將使用許可資料繳交日調至116年10月14日；後續答覆見附件。',
            ),
            '翠湖泵站更新工程使用許可資料繳交調整答覆\n同意前函所請，資料繳交期限改為116年10月14日。',
        ],
    },
    {
        'name': 'conditional_task_without_anchor', 'mode': 'review',
        'sources': [
            letter(
                '白石水門更新工程', '啟用準備。',
                '使用許可核准後三日內完成設備啟用。核准結果及日期目前尚未確定。',
            ),
        ],
    },
    {
        'name': 'overnight_interval', 'mode': 'dated', 'dates': ['2027/12/31'], 'times': ['22:00'],
        'end_dates': ['2028/01/01'], 'end_times': ['05:00'],
        'sources': [
            letter(
                '青田配電更新工程', '停電切換安排。',
                '116年12月31日22時至次日5時辦理停電切換，請維運團隊現場配合。',
            ),
        ],
    },
    {
        'name': 'direct_deadline', 'mode': 'dated', 'dates': ['2027/03/12'],
        'sources': [
            letter(
                '北港倉儲改善工程', '請交付設備校驗成果。',
                '設備校驗成果須於116年3月12日送達本中心。',
            ),
        ],
    },
    {
        'name': 'paraphrase_new_project', 'mode': 'dated', 'dates': ['2027/04/23'],
        'sources': [
            letter(
                '青嶼配水改善工程', '校驗成果移交事宜。',
                '本次成果的最遲交付日訂為116年4月23日，請維運團隊據此安排。',
            ),
        ],
    },
    {
        'name': 'date_only_in_attachment', 'mode': 'dated', 'dates': ['2027/05/06'], 'times': ['14:20'],
        'sources': [
            letter(
                '西灣配電改善工程', '現場聯合測試安排。',
                '本次聯合測試依隨函附頁時程辦理，請相關人員到場。',
            ),
            '西灣配電改善工程本次聯合測試附頁\n執行時段：116年5月6日下午2時20分至3時40分\n集合位置：控制室。',
        ],
    },
    {
        'name': 'relative_anchor_missing', 'mode': 'review',
        'sources': [
            letter(
                '南岬廠房改善工程', '設備缺失處置要求。',
                '此通知送達承辦單位起算三個工作日內，應完成設備缺失處置。文件未記載實際送達時間。',
            ),
        ],
    },
    {
        'name': 'urgency_without_date', 'mode': 'review',
        'sources': [
            letter(
                '雲丘場館改善工程', '資料補正要求。',
                '相關佐證目前仍有缺漏，請優先補齊後交本中心續辦；完成時點尚未排定。',
            ),
        ],
    },
    {
        'name': 'historical_only', 'mode': 'none',
        'sources': [
            letter(
                '松原站區改善工程', '歷次設備測試紀錄存查。',
                '115年11月18日設備測試已完成且缺失均已結清。本次僅供存查，不需另行辦理。',
            ),
        ],
    },
    {
        'name': 'cancelled_event', 'mode': 'none',
        'sources': [
            letter(
                '海澄碼頭改善工程', '撤銷現場活動安排。',
                '原排定116年6月7日上午9時至10時之現場觀摩現予撤銷，不另排替代活動，無須到場。',
            ),
        ],
    },
    {
        'name': 'proposal_not_approved', 'mode': 'review',
        'sources': [
            letter(
                '向陽機房改善工程', '擬調整資料交付時間。',
                '建議改以116年7月8日交付資料，尚待對方同意；在取得回覆前不得視為確定交付日。',
            ),
        ],
    },
    {
        'name': 'two_independent_tasks', 'mode': 'dated', 'dates': ['2027/08/09', '2027/08/13'],
        'sources': [
            letter(
                '望野校舍改善工程', '本次交付與演練安排。',
                '一、檢核表請於116年8月9日交付本中心。\n二、116年8月13日上午10時至11時辦理現場演練，請準時到場。',
            ),
        ],
    },
    {
        'name': 'same_day_sequential_agenda_stages', 'mode': 'dated',
        'dates': ['2027/09/18', '2027/09/18', '2027/09/18'], 'times': ['09:50', '10:30', '11:30'],
        'sources': [
            letter(
                '柳川橋梁整建工程', '聯合查核安排。',
                '116年9月18日上午9時50分於會議室辦理簡報及注意事項告知；預定10時30分於工地現場辦理查核；預定11時30分返回會議室辦理文件檢討。',
            ),
        ],
    },
    {
        'name': 'unusual_heading_with_date', 'mode': 'dated', 'dates': ['2027/09/15'],
        'sources': [
            letter(
                '星洲水池改善工程', '試驗報告交付安排。',
                '協作節點：116年9月15日完成試驗報告交付。\n沿革：先前討論發生於115年12月21日，僅列為背景資料。',
            ),
        ],
    },
]

# New contrast inputs reserved for the real-document fix acceptance run.
# Dates/projects differ from the production corpus; expected values are not prompts.
HOLDOUT_CASES = [
    {
        'name': 'shared_deliverable_not_one_event_per_role', 'mode': 'mixed', 'dates': ['2027/04/18'],
        'responsible_party': '管理處',
        'sources': [
            letter(
                '林渡淨水設施改善工程', '缺失修復成果追蹤。',
                '維運團隊應修复現場缺失。副本送管理處：請督促維運團隊完成改善，於116年4月18日前彙送同一修復成果及影像；此為本次共同交付節點。',
            ),
        ],
    },
    {
        'name': 'two_deliverables_with_same_day_are_not_role_duplicates', 'mode': 'dated',
        'dates': ['2027/04/18', '2027/04/18'],
        'sources': [
            letter(
                '赤羽站區更新工程', '兩项獨立成果交付。',
                '116年4月18日維運團隊須交付供電系統測試報告；同日另須交付消防系統演練紀錄。兩項工作獨立驗收，不得合併。',
            ),
        ],
    },
    {
        'name': 'announcement_unknown_activity_fixed', 'mode': 'mixed', 'dates': ['2027/07/21'],
        'times': ['21:30'], 'end_dates': ['2027/07/22'], 'end_times': ['04:30'],
        'sources': [
            letter(
                '沙洲通道維修工程', '施工及通知安排。',
                '本工程於116年7月21日21時30分至次日4時30分施工。請維運團隊到場執行，並另行通知使用單位；通知時間尚未排定。',
            ),
        ],
    },
    {
        'name': 'issue_date_does_not_anchor_receipt_deadline', 'mode': 'review',
        'sources': [
            letter(
                '石川抽水設備工程', '補正要求。',
                '請維運團隊在收到本通知後五日內補正資料。送達日期尚待確認，不得依發文時間起算。',
            ),
        ],
    },
]

# Frozen after the generic prompt/flow change; never interpolated into prompts.
BLIND_CASES = [
    {
        'name': 'explicit_executor_not_delivery_destination', 'mode': 'dated', 'dates': ['2028/02/17'],
        'responsible_party': '驗證室',
        'sources': [
            letter(
                '杉溪儀控整建工程', '成果送驗安排。',
                '本工程的量測資料由驗證室於117年2月17日前交予維運團隊，維運團隊為接收窗口。',
            ),
        ],
    },
    {
        'name': 'swapped_executor_not_delivery_destination', 'mode': 'dated', 'dates': ['2028/02/17'],
        'responsible_party': '維運團隊',
        'sources': [
            letter(
                '杉溪儀控整建工程', '成果送驗安排。',
                '本工程的量測資料由維運團隊於117年2月17日前交予驗證室，驗證室為接收窗口。',
            ),
        ],
    },
    {
        'name': 'related_annex_reschedules_existing_activity', 'mode': 'dated', 'dates': ['2028/02/23'],
        'times': ['08:40'], 'end_dates': ['2028/02/23'], 'end_times': ['10:10'],
        'sources': [
            letter(
                '榕岸閘門改善工程', '連動試車時程。',
                '連動試車原訂117年2月20日8時40分至10時10分，最新核定調整見附件，請按新時程到場。',
            ),
            '榕岸閘門改善工程連動試車調整通知\n原排117年2月20日的連動試車移至117年2月23日，時間仍為8時40分至10時10分。此調整已核定，原時程不再執行。',
        ],
    },
    {
        'name': 'unrelated_annex_date_is_not_task_deadline', 'mode': 'review',
        'sources': [
            letter(
                '棠浦照明汰換工程', '校準文件需求。',
                '請提出本工程校準報告，交件時間另行通知。附件是另一工程的既往資料，僅供格式參考。',
            ),
            '槐山庫區整修工程舊案\n116年1月19日已繳交結案報告，毋須辦理新工作。',
        ],
    },
]

# Additional role/date contrasts for the September 11 fix. These answers are
# evaluated outside model input, never included in production instructions.
ROLE_DATE_CASES = [
    {
        'name': 'receipt_missing_without_explicit_unknown_disclaimer', 'mode': 'review',
        'sources': [
            letter(
                '澄原排水設備更新工程', '檢核資料補件。',
                '維運團隊收到本件時須開始公開檢核摘要，連續張貼七日。',
            ),
        ],
    },
    {
        'name': 'dated_activity_same_day_as_issue_is_still_valid', 'mode': 'dated',
        'dates': ['2027/03/01'], 'times': ['14:00'], 'end_dates': ['2027/03/01'], 'end_times': ['15:00'],
        'sources': [
            letter(
                '澄原排水設備更新工程', '檢核作業安排。',
                '維運團隊於116年3月1日14時至15時辦理檢核作業。',
            ),
        ],
    },
    {
        'name': 'local_copy_addressee_owns_delivery_deadline', 'mode': 'mixed',
        'dates': ['2028/06/22'], 'responsible_party': '品質管理組',
        'sources': [
            letter(
                '沐川通風設施整修工程', '修復及成果移交。',
                '維運團隊應完成現場修復，施工日另定。\n副本致品質管理組：請督促修復及辦理複核，並於117年6月22日前彙整成果交本中心。',
            ),
        ],
    },
    {
        'name': 'recipient_owns_delivery_copy_is_only_recipient', 'mode': 'dated',
        'dates': ['2028/06/22'], 'responsible_party': '維運團隊',
        'sources': [
            letter(
                '沐川通風設施整修工程', '成果移交。',
                '維運團隊須於117年6月22日前彙整成果送交品質管理組。副本致品質管理組供收件備查，不另交辦工作。',
            ),
        ],
    },
]
